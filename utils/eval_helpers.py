import cv2
import os
import torch
import torch.nn.functional as F
from tqdm import tqdm
import numpy as np
import matplotlib.pyplot as plt

from datasets.gradslam_datasets.geometryutils import relative_transformation
from utils.camera_helpers import setup_camera
from utils.slam_external import build_rotation, calc_psnr
from utils.slam_helpers import (
    transform_to_frame,
    transformed_params2rendervar,
    transformed_params2depthplussilhouette,
    get_renderings,
)

from diff_gaussian_rasterization import GaussianRasterizer as Renderer

from pytorch_msssim import ms_ssim
from torchmetrics.image.lpip import LearnedPerceptualImagePatchSimilarity
loss_fn_alex = LearnedPerceptualImagePatchSimilarity(net_type='alex', normalize=True).cuda()

import imageio.v2 as iio
import numpy as np

import imageio
import glob
import open3d as o3d
import wandb
from sklearn.decomposition import PCA

  
def make_vid(input_path): 
    
    os.environ['IMAGEIO_FFMPEG_EXE'] = '/usr/bin/ffmpeg'

    images = list()
    for f in glob.glob(f'{input_path}/*'):
        images.append(imageio.imread(f))

    writer = imageio.get_writer(input_path + '.mp4', fps=30)
    print(input_path + '.mp4')
    print(images)
    for im in images:
        print('adding')
        # im is numpy array
        writer.append_data(im)
    writer.close()


def report_loss(
        losses,
        wandb_run,
        wandb_step,
        cam_tracking=False,
        obj_tracking=False,
        delta_optim=False,
        init_next=False):

    # Update loss dict
    if cam_tracking:
        tracking_loss_dict = {}
        for k, v in losses.items():
            tracking_loss_dict[f"Per Iteration Cam Tracking/{k}"] = v.item()
        tracking_loss_dict['Per Iteration Cam Tracking/step'] = wandb_step
        wandb_run.log(tracking_loss_dict)
    elif obj_tracking:
        tracking_loss_dict = {}
        for k, v in losses.items():
            tracking_loss_dict[f"Per Iteration Object Tracking/{k}"] = v.item()
        tracking_loss_dict['Per Iteration Object Tracking/step'] = wandb_step
        wandb_run.log(tracking_loss_dict)
    elif init_next:
        tracking_loss_dict = {}
        for k, v in losses.items():
            tracking_loss_dict[f"Per Iteration Init Next/{k}"] = v.item()
        tracking_loss_dict['Per Iteration Init Next/step'] = wandb_step
        wandb_run.log(tracking_loss_dict)
    elif delta_optim:
        delta_loss_dict = {}
        for k, v in losses.items():
            delta_loss_dict[f"Per Iteration Delta Optim/{k}"] = v.item()
        delta_loss_dict['Per Iteration Delta Optim/step'] = wandb_step
        wandb_run.log(delta_loss_dict)
    
    # Increment wandb step
    wandb_step += 1
    return wandb_step
        

def plot_rgbd_silhouette(color, depth, rastered_color, rastered_depth, presence_sil_mask, diff_depth_l1,
                         psnr, depth_l1, fig_title, plot_dir=None, plot_name=None, 
                         save_plot=False, wandb_run=None, wandb_step=None, wandb_title=None, diff_rgb=None):
    # Determine Plot Aspect Ratio
    aspect_ratio = color.shape[2] / color.shape[1]
    fig_height = 8
    fig_width = 14/1.55
    fig_width = fig_width * aspect_ratio
    # Plot the Ground Truth and Rasterized RGB & Depth, along with Diff Depth & Silhouette
    fig, axs = plt.subplots(2, 3, figsize=(fig_width, fig_height))
    axs[0, 0].imshow(color.cpu().permute(1, 2, 0))
    axs[0, 0].set_title("Ground Truth RGB")
    axs[0, 1].imshow(depth[0, :, :].cpu(), cmap='jet', vmin=0, vmax=6)
    axs[0, 1].set_title("Ground Truth Depth")
    rastered_color = torch.clamp(rastered_color, 0, 1)
    axs[1, 0].imshow(rastered_color.cpu().permute(1, 2, 0))
    axs[1, 0].set_title("Rasterized RGB, PSNR: {:.2f}".format(psnr))
    axs[1, 1].imshow(rastered_depth[0, :, :].cpu(), cmap='jet', vmin=0, vmax=6)
    axs[1, 1].set_title("Rasterized Depth, L1: {:.2f}".format(depth_l1))
    if diff_rgb is not None:
        axs[0, 2].imshow(diff_rgb.cpu(), cmap='jet', vmin=0, vmax=6)
        axs[0, 2].set_title("Diff RGB L1")
    else:
        axs[0, 2].imshow(presence_sil_mask, cmap='gray')
        axs[0, 2].set_title("Rasterized Silhouette")
    diff_depth_l1 = diff_depth_l1.cpu().squeeze(0)
    axs[1, 2].imshow(diff_depth_l1, cmap='jet', vmin=0, vmax=6)
    axs[1, 2].set_title("Diff Depth L1")
    for ax in axs.flatten():
        ax.axis('off')
    fig.suptitle(fig_title, y=0.95, fontsize=16)
    fig.tight_layout()
    if save_plot:
        save_path = os.path.join(plot_dir, f"{plot_name}.png")
        plt.savefig(save_path, bbox_inches='tight')
    if wandb_run is not None:
        if wandb_step is None:
            wandb_run.log({wandb_title: fig})
        else:
            wandb_run.log({wandb_title: fig}, step=wandb_step)
    plt.close()


def report_progress(params, data, i, progress_bar, iter_time_idx, sil_thres, every_i=1, qual_every_i=1, 
                    tracking=False, mapping=False, wandb_run=None, wandb_step=None, wandb_save_qual=False, online_time_idx=None,
                    global_logging=True):
    if i % every_i == 0 or i == 1:
        if wandb_run is not None:
            if tracking:
                stage = "Tracking"
            elif mapping:
                stage = "Mapping"
            else:
                stage = "Current Frame Optimization"
        if not global_logging:
            stage = "Per Iteration " + stage

        if tracking:
            # Get list of gt poses
            gt_w2c_list = data['iter_gt_w2c_list']
            valid_gt_w2c_list = []
            
            # Get latest trajectory
            latest_est_w2c = data['w2c']
            latest_est_w2c_list = []
            latest_est_w2c_list.append(latest_est_w2c)
            valid_gt_w2c_list.append(gt_w2c_list[0])
            for idx in range(1, iter_time_idx+1):
                # Check if gt pose is not nan for this time step
                if torch.isnan(gt_w2c_list[idx]).sum() > 0:
                    continue
                interm_cam_rot = F.normalize(params['cam_unnorm_rots'][..., idx].detach())
                interm_cam_trans = params['cam_trans'][..., idx].detach()
                intermrel_w2c = torch.eye(4).cuda().float()
                intermrel_w2c[:3, :3] = build_rotation(interm_cam_rot)
                intermrel_w2c[:3, 3] = interm_cam_trans
                latest_est_w2c = intermrel_w2c
                latest_est_w2c_list.append(latest_est_w2c)
                valid_gt_w2c_list.append(gt_w2c_list[idx])

            # Get latest gt pose
            gt_w2c_list = valid_gt_w2c_list
            iter_gt_w2c = gt_w2c_list[-1]
            # Get euclidean distance error between latest and gt pose
            iter_pt_error = torch.sqrt((latest_est_w2c[0,3] - iter_gt_w2c[0,3])**2 + (latest_est_w2c[1,3] - iter_gt_w2c[1,3])**2 + (latest_est_w2c[2,3] - iter_gt_w2c[2,3])**2)
            if iter_time_idx > 0:
                # Calculate relative pose error
                rel_gt_w2c = relative_transformation(gt_w2c_list[-2], gt_w2c_list[-1])
                rel_est_w2c = relative_transformation(latest_est_w2c_list[-2], latest_est_w2c_list[-1])
                rel_pt_error = torch.sqrt((rel_gt_w2c[0,3] - rel_est_w2c[0,3])**2 + (rel_gt_w2c[1,3] - rel_est_w2c[1,3])**2 + (rel_gt_w2c[2,3] - rel_est_w2c[2,3])**2)
            else:
                rel_pt_error = torch.zeros(1).float()
            
            # Calculate ATE RMSE
            ate_rmse = evaluate_ate(gt_w2c_list, latest_est_w2c_list)
            ate_rmse = np.round(ate_rmse, decimals=6)
            if wandb_run is not None:
                tracking_log = {f"{stage}/Latest Pose Error":iter_pt_error, 
                               f"{stage}/Latest Relative Pose Error":rel_pt_error,
                               f"{stage}/ATE RMSE":ate_rmse}

        # Get current frame Gaussians
        transformed_gaussians = transform_to_frame(params, iter_time_idx, 
                                                   gaussians_grad=False,
                                                   camera_grad=False)

        # Initialize Render Variables
        rendervar = transformed_params2rendervar(params, transformed_gaussians)
        depth_sil_rendervar = transformed_params2depthplussilhouette(params, data['w2c'], 
                                                                     transformed_gaussians)
        depth_sil, _, _, _, _ = Renderer(raster_settings=data['cam'])(**depth_sil_rendervar)
        rastered_depth = depth_sil[0, :, :].unsqueeze(0)
        valid_depth_mask = (data['depth'] > 0)
        silhouette = depth_sil[1, :, :]
        presence_sil_mask = (silhouette > sil_thres)

        im, _, _, _, _ = Renderer(raster_settings=data['cam'])(**rendervar)
        if tracking:
            psnr = calc_psnr(im * presence_sil_mask, data['im'] * presence_sil_mask).mean()
        else:
            psnr = calc_psnr(im, data['im']).mean()

        if tracking:
            diff_depth_rmse = torch.sqrt((((rastered_depth - data['depth']) * presence_sil_mask) ** 2))
            diff_depth_rmse = diff_depth_rmse * valid_depth_mask
            rmse = diff_depth_rmse.sum() / valid_depth_mask.sum()
            diff_depth_l1 = torch.abs((rastered_depth - data['depth']) * presence_sil_mask)
            diff_depth_l1 = diff_depth_l1 * valid_depth_mask
            depth_l1 = diff_depth_l1.sum() / valid_depth_mask.sum()
        else:
            diff_depth_rmse = torch.sqrt((((rastered_depth - data['depth'])) ** 2))
            diff_depth_rmse = diff_depth_rmse * valid_depth_mask
            rmse = diff_depth_rmse.sum() / valid_depth_mask.sum()
            diff_depth_l1 = torch.abs((rastered_depth - data['depth']))
            diff_depth_l1 = diff_depth_l1 * valid_depth_mask
            depth_l1 = diff_depth_l1.sum() / valid_depth_mask.sum()

        if not (tracking or mapping):
            progress_bar.set_postfix({f"Time-Step: {iter_time_idx} | PSNR: {psnr:.{7}} | Depth RMSE: {rmse:.{7}} | L1": f"{depth_l1:.{7}}"})
            progress_bar.update(every_i)
        elif tracking:
            progress_bar.set_postfix({f"Time-Step: {iter_time_idx} | Rel Pose Error: {rel_pt_error.item():.{7}} | Pose Error: {iter_pt_error.item():.{7}} | ATE RMSE": f"{ate_rmse.item():.{7}}"})
            progress_bar.update(every_i)
        elif mapping:
            progress_bar.set_postfix({f"Time-Step: {online_time_idx} | Frame {data['id']} | PSNR: {psnr:.{7}} | Depth RMSE: {rmse:.{7}} | L1": f"{depth_l1:.{7}}"})
            progress_bar.update(every_i)
        
        if wandb_run is not None:
            wandb_log = {f"{stage}/PSNR": psnr,
                         f"{stage}/Depth RMSE": rmse,
                         f"{stage}/Depth L1": depth_l1,
                         f"{stage}/step": wandb_step}
            if tracking:
                wandb_log = {**wandb_log, **tracking_log}
            wandb_run.log(wandb_log)
        
        if wandb_save_qual and (i % qual_every_i == 0 or i == 1):
            # Silhouette Mask
            presence_sil_mask = presence_sil_mask.detach().cpu().numpy()

            # Log plot to wandb
            if not mapping:
                fig_title = f"Time-Step: {iter_time_idx} | Iter: {i} | Frame: {data['id']}"
            else:
                fig_title = f"Time-Step: {online_time_idx} | Iter: {i} | Frame: {data['id']}"
            plot_rgbd_silhouette(data['im'], data['depth'], im, rastered_depth, presence_sil_mask, diff_depth_l1,
                                 psnr, depth_l1, fig_title, wandb_run=wandb_run, wandb_step=wandb_step, 
                                 wandb_title=f"{stage} Qual Viz")


def tensor2param(tensor):
    if not isinstance(tensor, torch.Tensor):
        return torch.nn.Parameter(torch.tensor(tensor).cuda().float().contiguous().requires_grad_(True))
    else:
        return torch.nn.Parameter(tensor.cuda().float().contiguous().requires_grad_(True))

def param2tensor(param):
    return torch.nn.Tensor(param.cuda().float())


def eval(
        dataset,
        final_params,
        num_frames,
        eval_dir,
        sil_thres, 
        wandb_run=None,
        wandb_save_qual=False, 
        eval_every=1,
        save_frames=True,
        variables=None,
        save_pc=True,
        save_videos=False,
        mov_thresh=0.001,
        vis_gt=False,
        rendered_motion=False,
        rendered_mov=False,
        rendered_silhouette=False,
        rendered_instseg=False,
        get_embeddings=False,
        rendered_bg=False,
        time_window=1):

    print("Evaluating Final Parameters ...")
    psnr_list = []
    rmse_list = []
    l1_list = []
    lpips_list = []
    ssim_list = []
    plot_dir = os.path.join(eval_dir, "plots")
    os.makedirs(plot_dir, exist_ok=True)
    if save_frames:
        render_rgb_dir = os.path.join(eval_dir, "rendered_rgb")
        os.makedirs(render_rgb_dir, exist_ok=True)
        render_depth_dir = os.path.join(eval_dir, "rendered_depth")
        os.makedirs(render_depth_dir, exist_ok=True)
        if rendered_mov:
            render_mov_dir = os.path.join(eval_dir, "rendered_mov")
            os.makedirs(render_mov_dir, exist_ok=True)
        render_emb_dir = os.path.join(eval_dir, "pca_emb")
        os.makedirs(render_emb_dir, exist_ok=True)
        render_emb_gt_dir = os.path.join(eval_dir, "pca_emb_gt")
        os.makedirs(render_emb_gt_dir, exist_ok=True)
        if rendered_instseg:
            render_instseg_dir = os.path.join(eval_dir, "rendered_instseg")
            os.makedirs(render_instseg_dir, exist_ok=True)
        if rendered_silhouette:
            render_sil_dir = os.path.join(eval_dir, "rendered_sil")
            os.makedirs(render_sil_dir, exist_ok=True)
        if save_pc:
            pc_dir = os.path.join(eval_dir, "pc")
            os.makedirs(pc_dir, exist_ok=True)
        if rendered_motion:
            render_motion_dir = os.path.join(eval_dir, "rendered_motion")
            os.makedirs(render_motion_dir, exist_ok=True)
        if rendered_bg:
            render_bg_dir = os.path.join(eval_dir, "rendered_bg")
            os.makedirs(render_bg_dir, exist_ok=True)
        if vis_gt:
            rgb_dir = os.path.join(eval_dir, "rgb")
            os.makedirs(rgb_dir, exist_ok=True)
            depth_dir = os.path.join(eval_dir, "depth")
            os.makedirs(depth_dir, exist_ok=True)
            instseg_dir = os.path.join(eval_dir, "instseg")
            os.makedirs(instseg_dir, exist_ok=True)

    gt_w2c_list = []
    import copy
    means2d = None
    pca = None
    for time_idx in tqdm(range(num_frames)):
        final_params_time = copy.deepcopy(final_params)
         # Get RGB-D Data & Camera Parameters
        data = dataset[time_idx]
        color, depth, intrinsics, pose, instseg, embeddings, support_trajs = data[0], data[1], data[2], data[3], data[4], data[5], data[6]
                
        gt_w2c = torch.linalg.inv(pose)
        gt_w2c_list.append(gt_w2c)
        intrinsics = intrinsics[:3, :3]

        if time_idx == 0:
            # Process Camera Parameters
            first_frame_w2c = torch.linalg.inv(pose)
            # Setup Camera
            cam = setup_camera(color.shape[2], color.shape[1], intrinsics.cpu().numpy(), first_frame_w2c.detach().cpu().numpy())
        
        # Skip frames if not eval_every
        if time_idx != 0 and (time_idx+1) % eval_every != 0:
            continue

        # Define current frame data
        curr_data = {
            'cam': cam,
            'im': color,
            'depth': depth,
            'id': time_idx,
            'intrinsics': intrinsics,
            'w2c': first_frame_w2c,
            'instseg': instseg,
            'embeddings': embeddings,
            'support_trajs': support_trajs}

        variables, im, _, rastered_depth, rastered_inst, mask, transformed_gaussians, means2d, visible, weight, rastered_motion2d, time_mask, rastered_moving, rastered_sil, rendered_embeddings, rastered_bg = get_renderings(
            final_params_time,
            variables,
            time_idx,
            curr_data,
            {'sil_thres': sil_thres, 'use_sil_for_loss': False, 'use_flow': 'rendered'},
            mov_thresh=mov_thresh, 
            disable_grads=True,
            track_cam=False,
            get_seg=True,
            get_motion=True,
            prev_means2d=means2d,
            get_embeddings=get_embeddings,
            time_window=time_window)
        
        if time_idx == 0:
            time_mask_0 = time_idx
        
        valid_depth_mask = (curr_data['depth'] > 0)
        rastered_depth_viz = rastered_depth.detach()
        rastered_inst_viz = rastered_inst.detach()
        rastered_sil_vis = rastered_sil.detach()
        rastered_moving_viz = rastered_moving.detach()
        rastered_depth = rastered_depth * valid_depth_mask
        presence_sil_mask = (rastered_sil > sil_thres)
        rastered_sil = rastered_sil.unsqueeze(0)
        if time_idx > 0:
            rastered_motion2d_viz = rastered_motion2d.detach()
        
        # Render RGB and Calculate PSNR
        weighted_im = im * valid_depth_mask
        weighted_gt_im = curr_data['im'] * valid_depth_mask
        psnr = calc_psnr(weighted_im, weighted_gt_im).mean()
        try:
            ssim = ms_ssim(weighted_im.unsqueeze(0).cpu(), weighted_gt_im.unsqueeze(0).cpu(), 
                        data_range=1.0, size_average=True)
        except:
            ssim = torch.tensor(0)
        lpips_score = loss_fn_alex(torch.clamp(weighted_im.unsqueeze(0), 0.0, 1.0),
                                    torch.clamp(weighted_gt_im.unsqueeze(0), 0.0, 1.0)).item()

        psnr_list.append(psnr.cpu().numpy())
        ssim_list.append(ssim.cpu().numpy())
        lpips_list.append(lpips_score)

        # Compute Depth RMSE
        diff_depth_rmse = torch.sqrt((((rastered_depth - curr_data['depth'])) ** 2))
        diff_depth_rmse = diff_depth_rmse * valid_depth_mask
        rmse = diff_depth_rmse.sum() / valid_depth_mask.sum()
        diff_depth_l1 = torch.abs((rastered_depth - curr_data['depth']))
        diff_depth_l1 = diff_depth_l1 * valid_depth_mask
        depth_l1 = diff_depth_l1.sum() / valid_depth_mask.sum()
        rmse_list.append(rmse.cpu().numpy())
        l1_list.append(depth_l1.cpu().numpy())

        if save_frames:
            # Save Rendered RGB and Depth
            viz_render_im = torch.clamp(im, 0, 1)
            viz_render_im = viz_render_im.detach().cpu().permute(1, 2, 0).numpy()
            cv2.imwrite(os.path.join(render_rgb_dir, "gs_{:04d}.png".format(time_idx)), cv2.cvtColor(viz_render_im*255, cv2.COLOR_RGB2BGR))

            # depth
            viz_render_depth = rastered_depth_viz[0].detach().cpu().numpy()
            vmin = 0
            vmax = viz_render_depth.max()
            normalized_depth = np.clip((viz_render_depth - vmin) / (vmax - vmin), 0, 1)
            depth_colormap = cv2.applyColorMap((normalized_depth * 255).astype(np.uint8), cv2.COLORMAP_JET)
            cv2.imwrite(os.path.join(render_depth_dir, "gs_{:04d}.png".format(time_idx)), depth_colormap)

            # moving
            if rendered_mov:
                rastered_moving_viz = rastered_moving_viz[0].detach().cpu().numpy()
                smax, smin = rastered_moving_viz.max(), rastered_moving_viz.min()
                normalized_mov = np.clip((rastered_moving_viz - smin) / (smax - smin), 0, 1)
                moving_colormap = cv2.applyColorMap((normalized_mov * 255).astype(np.uint8), cv2.COLORMAP_JET)
                cv2.imwrite(os.path.join(render_mov_dir, "gs_{:04d}.png".format(time_idx)), moving_colormap)

            # bg
            if rendered_bg:
                rastered_bg = rastered_bg[0].detach().cpu().numpy()
                smax, smin = rastered_bg.max(), rastered_bg.min()
                normalized_bg = np.clip((rastered_bg - smin) / (smax - smin), 0, 1)
                bg_colormap = cv2.applyColorMap((normalized_bg * 255).astype(np.uint8), cv2.COLORMAP_JET)
                cv2.imwrite(os.path.join(render_bg_dir, "gs_{:04d}.png".format(time_idx)), bg_colormap)

            # embeddings
            rendered_embeddings = rendered_embeddings.permute(1, 2, 0).detach().cpu().numpy()
            shape = rendered_embeddings.shape
            if shape[2] != 3:
                if pca is None:
                    pca = PCA(n_components=3)
                    pca.fit(rendered_embeddings.reshape(-1, shape[2]))
                rendered_embeddings = pca.transform(
                    rendered_embeddings.reshape(-1, shape[2]))
                rendered_embeddings = rendered_embeddings.reshape(
                    (shape[0], shape[1], 3))
            smax, smin = rendered_embeddings.max(), rendered_embeddings.min()
            normalized_emb = np.clip((rendered_embeddings - smin) / (smax - smin), 0, 1)
            emb_colormap = (normalized_emb * 255).astype(np.uint8)
            imageio.imwrite(os.path.join(render_emb_dir, "gs_{:04d}.png".format(time_idx)), emb_colormap)

            rendered_embeddings = curr_data['embeddings'].permute(1, 2, 0).detach().cpu().numpy()
            if shape[2] != 3:
                shape = rendered_embeddings.shape
                rendered_embeddings = pca.transform(
                    rendered_embeddings.reshape(-1, shape[2]))
                rendered_embeddings = rendered_embeddings.reshape(
                    (shape[0], shape[1], 3))
            smax, smin = rendered_embeddings.max(), rendered_embeddings.min()
            normalized_emb = np.clip((rendered_embeddings - smin) / (smax - smin), 0, 1)
            emb_colormap = (normalized_emb * 255).astype(np.uint8)
            imageio.imwrite(os.path.join(render_emb_gt_dir, "gs_{:04d}.png".format(time_idx)), emb_colormap)

            if rendered_silhouette:
                # silouette
                rastered_sil_vis = torch.clamp(rastered_sil_vis , 0, 1)[0].detach().cpu().numpy()
                sil_colormap = (rastered_sil_vis * 255).astype(np.uint8)
                cv2.imwrite(os.path.join(render_sil_dir, "gs_{:04d}.png".format(time_idx)), sil_colormap)

            if rendered_instseg:
                # instseg
                viz_render_instseg = rastered_inst_viz[0].detach().cpu().numpy()
                smax, smin = viz_render_instseg.max(), viz_render_instseg.min()
                normalized_instseg = np.clip((viz_render_instseg - smin) / (smax - smin), 0, 1)
                instseg_colormap = cv2.applyColorMap((normalized_instseg * 255).astype(np.uint8), cv2.COLORMAP_JET)
                cv2.imwrite(os.path.join(render_instseg_dir, "gs_{:04d}.png".format(time_idx)), instseg_colormap)

            if rendered_motion and time_idx > 0:
                rastered_motion2d_viz = rastered_motion2d_viz.permute(1, 2, 0)
                # motion
                pix_x = torch.arange(rastered_motion2d_viz.shape[1]).unsqueeze(1).repeat((1, rastered_motion2d_viz.shape[0])).numpy().flatten()
                pix_y = torch.arange(rastered_motion2d_viz.shape[0]).unsqueeze(0).repeat((rastered_motion2d_viz.shape[1], 1)).numpy().flatten()
                flow_x = rastered_motion2d_viz[:, :, 0].clone().detach().cpu().numpy().flatten()
                flow_y = rastered_motion2d_viz[:, :, 1].clone().detach().cpu().numpy().flatten()
                stride = 25
                plt.imshow(curr_data['im'].cpu().permute(1, 2, 0).numpy())
                for i in range(0, flow_x.shape[0], stride):
                    plt.arrow(pix_x[i], pix_y[i], flow_x[i], flow_y[i], width=0.2)
                plt.margins(0, 0)
                plt.axis('off')
                plt.savefig(os.path.join(render_motion_dir, "flow_arrows_{:04d}.png".format(time_idx)), bbox_inches="tight")
                plt.close()

                # GT
                gt_flows = curr_data['support_trajs']
                pix_y = gt_flows[0, :, 1].clone().detach().cpu().numpy()
                pix_x = gt_flows[0, :, 0].clone().detach().cpu().numpy()
                flow_y = gt_flows[1, :, 1].clone().detach().cpu().numpy()-gt_flows[0, :, 1].clone().detach().cpu().numpy()
                flow_x = gt_flows[1, :, 0].clone().detach().cpu().numpy()-gt_flows[0, :, 0].clone().detach().cpu().numpy()
                stride = 1
                plt.imshow(curr_data['im'].cpu().permute(1, 2, 0).numpy())
                for i in range(0, flow_x.shape[0], stride):
                    # plt.arrow(supp[0, i, 0], supp[0, i, 1], flow[i, 1], flow[i, 0])
                    plt.arrow(pix_x[i], pix_y[i], flow_x[i], flow_y[i], width=0.2)
                plt.margins(0, 0)
                plt.axis('off')
                plt.savefig(os.path.join(render_motion_dir, "gt_flow_arrows_{:04d}.png".format(time_idx)), bbox_inches="tight")
                plt.close()
                
            if vis_gt:
                # Save GT RGB and Depth
                viz_gt_im = torch.clamp(curr_data['im'], 0, 1)
                viz_gt_im = viz_gt_im.detach().cpu().permute(1, 2, 0).numpy()
                # depth
                viz_gt_depth = curr_data['depth'][0].detach().cpu().numpy()
                vmin = 0 # viz_gt_depth.min() # 0
                vmax = viz_gt_depth.max() # 6
                imageio.imwrite('test.png', viz_gt_depth.astype(np.uint8))
                normalized_depth = np.clip((viz_gt_depth - vmin) / (vmax - vmin), 0, 1)
                imageio.imwrite('test_norm.png', normalized_depth.astype(np.uint8))
                depth_colormap = cv2.applyColorMap((normalized_depth * 255).astype(np.uint8), cv2.COLORMAP_JET)
                # instseg
                viz_gt_instseg = curr_data['instseg'][0].detach().cpu().numpy()
                smax, smin = viz_gt_instseg.max(), viz_gt_instseg.min()
                normalized_instseg = np.clip((viz_gt_instseg - smin) / (smax - smin), 0, 1)
                instseg_colormap = cv2.applyColorMap((normalized_instseg * 255).astype(np.uint8), cv2.COLORMAP_JET)

                cv2.imwrite(os.path.join(rgb_dir, "gt_{:04d}.png".format(time_idx)), cv2.cvtColor(viz_gt_im*255, cv2.COLOR_RGB2BGR))
                cv2.imwrite(os.path.join(depth_dir, "gt_{:04d}.png".format(time_idx)), depth_colormap)
                cv2.imwrite(os.path.join(instseg_dir, "gt_{:04d}.png".format(time_idx)), instseg_colormap)

        if save_pc:
            _mask = time_mask & variables['moving']
            print('moving', final_params_time['means3D'][:, :, time_idx][_mask].shape)
            pcd = o3d.geometry.PointCloud()
            v3d = o3d.utility.Vector3dVector
            pcd.points = v3d(final_params_time['means3D'][:, :, time_idx][_mask].cpu().numpy())
            o3d.io.write_point_cloud(filename=os.path.join(pc_dir, "pc_{:04d}_mov.xyz".format(time_idx)), pointcloud=pcd)
            _mask = time_mask & ~variables['moving']
            print('static', final_params_time['means3D'][:, :, time_idx][_mask].shape)
            pcd = o3d.geometry.PointCloud()
            v3d = o3d.utility.Vector3dVector
            pcd.points = v3d(final_params_time['means3D'][:, :, time_idx][_mask].cpu().numpy())
            o3d.io.write_point_cloud(filename=os.path.join(pc_dir, "pc_{:04d}_stat.xyz".format(time_idx)), pointcloud=pcd)
            print('all', final_params_time['means3D'][:, :, time_idx][time_mask].shape)
            pcd = o3d.geometry.PointCloud()
            v3d = o3d.utility.Vector3dVector
            pcd.points = v3d(final_params_time['means3D'][:, :, time_idx][time_mask].cpu().numpy())
            o3d.io.write_point_cloud(filename=os.path.join(pc_dir, "pc_{:04d}_all.xyz".format(time_idx)), pointcloud=pcd)
        
    if save_videos:
        for input_path in [render_rgb_dir, render_depth_dir, rgb_dir, depth_dir, pc_dir, render_instseg_dir, instseg_dir, render_sil_dir]:
            make_vid(input_path)

    try:
        # Compute the final ATE RMSE
        # Get the final camera trajectory
        num_frames = final_params['cam_unnorm_rots'].shape[-1]
        latest_est_w2c = first_frame_w2c
        latest_est_w2c_list = []
        latest_est_w2c_list.append(latest_est_w2c)
        valid_gt_w2c_list = []
        valid_gt_w2c_list.append(gt_w2c_list[0])
        for idx in range(1, num_frames):
            # Check if gt pose is not nan for this time step
            if torch.isnan(gt_w2c_list[idx]).sum() > 0:
                continue
            interm_cam_rot = F.normalize(final_params['cam_unnorm_rots'][..., idx].detach())
            interm_cam_trans = final_params['cam_trans'][..., idx].detach()
            intermrel_w2c = torch.eye(4).cuda().float()
            intermrel_w2c[:3, :3] = build_rotation(interm_cam_rot)
            intermrel_w2c[:3, 3] = interm_cam_trans
            latest_est_w2c = intermrel_w2c
            latest_est_w2c_list.append(latest_est_w2c)
            valid_gt_w2c_list.append(gt_w2c_list[idx])
        gt_w2c_list = valid_gt_w2c_list
        # Calculate ATE RMSE
        ate_rmse = evaluate_ate(gt_w2c_list, latest_est_w2c_list)
        print("Final Average ATE RMSE: {:.2f} cm".format(ate_rmse*100))
        if wandb_run is not None:
            wandb_run.log({"Final Stats/Avg ATE RMSE": ate_rmse,
                        "Final Stats/step": 1})
    except:
        ate_rmse = 100.0
        print('Failed to evaluate trajectory with alignment.')
    
    # Compute Average Metrics
    psnr_list = np.array(psnr_list)
    rmse_list = np.array(rmse_list)
    l1_list = np.array(l1_list)
    ssim_list = np.array(ssim_list)
    lpips_list = np.array(lpips_list)
    avg_psnr = psnr_list.mean()
    avg_rmse = rmse_list.mean()
    avg_l1 = l1_list.mean()
    avg_ssim = ssim_list.mean()
    avg_lpips = lpips_list.mean()
    print("Average PSNR: {:.2f}".format(avg_psnr))
    print("Average Depth RMSE: {:.2f} cm".format(avg_rmse*100))
    print("Average Depth L1: {:.2f} cm".format(avg_l1*100))
    print("Average MS-SSIM: {:.3f}".format(avg_ssim))
    print("Average LPIPS: {:.3f}".format(avg_lpips))

    if wandb_run is not None:
        wandb_run.log({"Final Stats/Average PSNR": avg_psnr, 
                       "Final Stats/Average Depth RMSE": avg_rmse,
                       "Final Stats/Average Depth L1": avg_l1,
                       "Final Stats/Average MS-SSIM": avg_ssim, 
                       "Final Stats/Average LPIPS": avg_lpips,
                       "Final Stats/step": 1})

    # Save metric lists as text files
    np.savetxt(os.path.join(eval_dir, "psnr.txt"), psnr_list)
    np.savetxt(os.path.join(eval_dir, "rmse.txt"), rmse_list)
    np.savetxt(os.path.join(eval_dir, "l1.txt"), l1_list)
    np.savetxt(os.path.join(eval_dir, "ssim.txt"), ssim_list)
    np.savetxt(os.path.join(eval_dir, "lpips.txt"), lpips_list)

    # Plot PSNR & L1 as line plots
    fig, axs = plt.subplots(1, 2, figsize=(12, 4))
    axs[0].plot(np.arange(len(psnr_list)), psnr_list)
    axs[0].set_title("RGB PSNR")
    axs[0].set_xlabel("Time Step")
    axs[0].set_ylabel("PSNR")
    axs[1].plot(np.arange(len(l1_list)), l1_list*100)
    axs[1].set_title("Depth L1")
    axs[1].set_xlabel("Time Step")
    axs[1].set_ylabel("L1 (cm)")
    fig.suptitle("Average PSNR: {:.2f}, Average Depth L1: {:.2f} cm, ATE RMSE: {:.2f} cm".format(avg_psnr, avg_l1*100, ate_rmse*100), y=1.05, fontsize=16)
    plt.savefig(os.path.join(eval_dir, "metrics.png"), bbox_inches='tight')
    if wandb_run is not None:
        wandb_run.log({"Eval/Metrics": fig})
    plt.close()


