import os
from utils.get_data import get_gt_traj, load_scene_data, get_cam_data
import numpy as np
import torch
from utils.camera_helpers import get_projection_matrix
from utils.two2threeD_helpers import three2two, unnormalize_points, normalize_points
from utils.tapnet_utils_viz import vis_tracked_points
from utils.camera_helpers import setup_camera
from utils.slam_helpers import transform_to_frame
import copy


def get_gs_traj_pts(
        proj_matrix,
        params,
        first_occurance,
        w,
        h,
        start_pixels,
        start_pixels_normalized=True,
        gauss_ids=None,
        use_norm_pix=False,
        use_round_pix=True,
        use_depth_indicator=False,
        do_scale=True,
        no_bg=False,
        search_fg_only=False,
        w2c=None):

    if gauss_ids is None or gauss_ids == np.array(None):
        if search_fg_only:
            fg_mask = (params['bg'] < 0.5).squeeze()
            num_gauss = params['means3D'].shape[0]
            first_occurance = first_occurance[fg_mask]
            for k in params.keys():
                try:
                    if params[k].shape[0] == num_gauss:
                        params[k] = params[k][fg_mask]
                except:
                    params[k] = params[k]
        means3D = params['means3D'][first_occurance==first_occurance.min().item()]
        # assign and get 3D trajectories
        means3D_t0 = means3D[:, :, 0]

        if not use_norm_pix:
            if use_round_pix:
                means2D_t0 = three2two(proj_matrix, means3D_t0, w, h, do_round=True, do_scale=do_scale)
                if start_pixels_normalized:
                    start_pixels = unnormalize_points(start_pixels, h, w, do_round=True, do_scale=do_scale)
            else:
                means2D_t0 = three2two(proj_matrix, means3D_t0, w, h, do_round=False, do_scale=do_scale).float()
                if start_pixels_normalized:
                    start_pixels = unnormalize_points(start_pixels, h, w, do_round=False, do_scale=do_scale).to(means2D_t0.device).float()
        else:
            means2D_t0 = three2two(proj_matrix, means3D_t0, w, h, do_normalize=True, do_scale=do_scale).float()
            if not start_pixels_normalized:
                start_pixels = normalize_points(start_pixels.float(), h, w)
            start_pixels = start_pixels.to(means2D_t0.device).float()

        gauss_ids = find_closest_to_start_pixels(
            means2D_t0,
            start_pixels,
            means3D=means3D_t0,
            opacity=params['logit_opacities'],
            norm=use_norm_pix or not use_round_pix,
            use_depth_indicator=use_depth_indicator,
            w2c=w2c)

    gs_traj_3D, logit_opacities, logit_sclaes, rgb_colors, unnorm_rotations, visibility = get_3D_trajs_for_track(
        gauss_ids, params, return_all=True, no_bg=no_bg)
    
    params_gs_traj_3D = copy.deepcopy(params)
    params_gs_traj_3D['means3D'] = gs_traj_3D
    params_gs_traj_3D['unnorm_rotations'] = unnorm_rotations
    # get 2D trajectories
    gs_traj_2D = list()
    for time in range(gs_traj_3D.shape[-1]):
        if gs_traj_3D[:, :, time].sum() == 0:
            continue
        transformed_gs_traj_3D = transform_to_frame(
                params_gs_traj_3D,
                time,
                gaussians_grad=False,
                camera_grad=False,
                delta=0)
        gs_traj_2D.append(
            three2two(proj_matrix, transformed_gs_traj_3D['means3D'], w, h, do_normalize=False))

    gs_traj_2D = torch.stack(gs_traj_2D).permute(1, 0, 2)
    gs_traj_3D = gs_traj_3D.permute(0, 2, 1)

    return gs_traj_2D, gs_traj_3D, gauss_ids, visibility


def find_closest(means2D, pix, means3D=None, opacity=None, use_min_z_dist=False, use_max_opacity=False, w2c=None):
    # print("USING use_min_z_dist", use_min_z_dist)
    if use_min_z_dist:
        # convert means 3D to camera KS
        if w2c is not None:
            means_ones = torch.ones(means3D.shape[0], 1).to(means3D.device).float()
            means3D_4 = torch.cat((means3D, means_ones), dim=1)
            w2c = torch.tensor(w2c)
            w2c = w2c.to(means3D.device).float()
            means3D = (w2c @ means3D_4.T).T[:, :3]
        valid_depth_mask = means3D[:, -1] > 0
        print(valid_depth_mask.shape, valid_depth_mask.sum())
        means2D = means2D[valid_depth_mask]
        means3D = means3D[valid_depth_mask]
        opacity = opacity[valid_depth_mask]
    count = 0
    for d_x, d_y in zip([0, 1, 0, -1, 0, 1, -1, 1, -1, -2, 2, 0, 0, -2, 2, -2, 2, -1, 1, 1, -1. -2, 2, -2, 2], [0, 0, 1, 0, -1, 1, -1, -1, 1, 0, 0, -2, 2, -1, 1, 1, -1, -2, 2, -2, 2, -2, -2, 2, 2]):
        pix_mask =  torch.logical_and(
            means2D[:, 0] == pix[0] + d_x,
            means2D[:, 1] == pix[1] + d_y)
        if pix_mask.sum() != 0:
            possible_ids = torch.nonzero(pix_mask)
            if use_min_z_dist:
                min_z_dist = torch.atleast_2d(means3D[possible_ids].squeeze())[:, -1].argmin()
                gauss_id = possible_ids[min_z_dist]
            elif use_max_opacity:
                max_opacity = torch.atleast_2d(opacity[possible_ids].squeeze()).argmax()
                gauss_id = possible_ids[max_opacity]
            else:
                gauss_id = possible_ids[0]
            return gauss_id
        count += 1
    return None


def find_closest_norm(means2D, pix):
    dist = torch.cdist(pix.unsqueeze(0).unsqueeze(0), means2D.unsqueeze(0)).squeeze()
    gauss_id = dist.argmin()
    return gauss_id


def find_closest_to_start_pixels(means2D, start_pixels, means3D=None, opacity=None, norm=False, use_depth_indicator=False, w2c=None):
    gauss_ids = list()
    gs_traj_3D = list()
    for i, pix in enumerate(start_pixels):
        if norm:
            gauss_id = find_closest_norm(means2D, pix).unsqueeze(0)
        else:
            gauss_id = find_closest(means2D, pix, means3D, opacity, use_min_z_dist=use_depth_indicator, w2c=w2c)

        if gauss_id is not None:
            gauss_ids.append(gauss_id)
        else:
            gauss_ids.append(torch.tensor([0]).to(means2D.device))
    return gauss_ids
        

def get_3D_trajs_for_track(gauss_ids, params, return_all=False, no_bg=False):
    gs_traj_3D = list()
    logit_opacities = list()
    logit_scales = list()
    rgb_colors = list()
    unnorm_rotations = list()
    visibility = list()

    for gauss_id in gauss_ids:
        if no_bg:
            bg_mask = params['bg'][gauss_id] < 0.5
        else:
            bg_mask = params['bg'][gauss_id] < 1000

        if gauss_id != -1 and bg_mask:
            gs_traj_3D.append(
                    params['means3D'][gauss_id].squeeze())
            unnorm_rotations.append(params['unnorm_rotations'][gauss_id].squeeze())
            logit_opacities.append(params['logit_opacities'][gauss_id].squeeze())
            rgb_colors.append(params['rgb_colors'][gauss_id])
            logit_scales.append(params['log_scales'][gauss_id])
            if 'visibility' in params.keys():
                visibility.append(params['visibility'][gauss_id].squeeze())
            else:
                visibility.append(
                    (torch.zeros(params['means3D'].shape[2])).to(params['means3D'].device))
        else:
            gs_traj_3D.append(
                    (torch.ones_like(params['means3D'][0]).squeeze()*-1).to(params['means3D'].device))
            logit_opacities.append(torch.tensor(-1).to(params['means3D'].device))
            rgb_colors.append(torch.tensor([[-1, -1, -1]]).to(params['means3D'].device))
            logit_scales.append(torch.tensor([[-1]]).to(params['means3D'].device))
            unnorm_rotations.append(
                (torch.ones_like(params['unnorm_rotations'][0]).squeeze()*-1).to(params['means3D'].device))
            visibility.append(
                    (torch.zeros(params['means3D'].shape[2])).to(params['means3D'].device))

    if return_all:
        return torch.stack(gs_traj_3D), torch.stack(logit_opacities), torch.stack(logit_scales), torch.stack(rgb_colors), torch.stack(unnorm_rotations), torch.stack(visibility)
    else:
        return torch.stack(gs_traj_3D)

def _eval_traj(
        params,
        first_occurance,
        data,
        h=None,
        w=None,
        proj_matrix=None,
        default_to_proj=True,
        use_only_pred=True,
        vis_trajs=False,
        results_dir=None,
        gauss_ids_to_track=None,
        dataset='jono',
        use_norm_pix=False,
        use_round_pix=False,
        use_depth_indicator=False,
        do_scale=False,
        w2c=None):

    if params['means3D'][:, :, -1].sum() == 0:
        params['means3D'] = params['means3D'][:, :, :-1]
        params['unnorm_rotations'] = params['unnorm_rotations'][:, :, :-1]

    # use projected 3D trajectories if no ground truth
    if data['points'].sum() == 0 and default_to_proj:
        gt_traj_2D = data['points_projected']
        occluded = data['occluded'] - 1 
    else:
        gt_traj_2D = data['points']
        occluded = data['occluded']
        if dataset == 'jono':
            gt_traj_2D[:, :, 0] = ((gt_traj_2D[:, :, 0] * w) - 1)/w
            gt_traj_2D[:, :, 1] = ((gt_traj_2D[:, :, 1] * h) - 1)/h
    
    if dataset == "jono":
        search_fg_only = True
    else:
        search_fg_only = False
    
    '''# filter valid depth (for Jono baselines needed)
    if dataset == "jono":
        valid_depth = params['means3D'][:, -1, 0] > 0
        num_gauss = params['means3D'].shape[0]
        first_occurance = first_occurance[valid_depth]
        for k in params.keys():
            try:
                if params[k].shape[0] == num_gauss:
                    params[k] = params[k][valid_depth]
            except:
                params[k] = params[k]'''

    valids = 1-occluded.float()
    
    # get trajectories of Gaussians
    gs_traj_2D, gs_traj_3D, gauss_ids, pred_visibility = get_gs_traj_pts(
        proj_matrix,
        params,
        first_occurance,
        w,
        h,
        gt_traj_2D[:, 0].clone(),
        start_pixels_normalized=True,
        gauss_ids=gauss_ids_to_track,
        use_norm_pix=use_norm_pix,
        use_round_pix=use_round_pix,
        use_depth_indicator=use_depth_indicator,
        do_scale=do_scale,
        no_bg=False,
        search_fg_only=search_fg_only, 
        w2c=w2c)

    # unnormalize gt to image pixels
    gt_traj_2D = unnormalize_points(gt_traj_2D, h, w)

    # make timesteps after predicted len invalid
    if use_only_pred:
        gt_traj_2D = gt_traj_2D[:, :gs_traj_2D.shape[1], :]
        valids = valids[:, :gs_traj_2D.shape[1]]
        occluded = occluded[:, :gs_traj_2D.shape[1]]

    # unsqueeze to batch dimension
    if len(gt_traj_2D.shape) == 3:
        gt_traj_2D = gt_traj_2D.unsqueeze(0)
        gs_traj_2D = gs_traj_2D.unsqueeze(0)
        valids = valids.unsqueeze(0)
        occluded = occluded.unsqueeze(0)
        pred_visibility = pred_visibility.unsqueeze(0)

    # mask by valid ids
    if valids.sum() != 0:
        gs_traj_2D, gt_traj_2D, valids, occluded, pred_visibility = mask_valid_ids(
            valids,
            gs_traj_2D,
            gt_traj_2D,
            occluded,
            pred_visibility)

    # compute metrics from pips
    pips_metrics = compute_metrics(
        h,
        w,
        gs_traj_2D.to(gt_traj_2D.device),
        gt_traj_2D,
        valids,
    )

    metrics = {'pips': pips_metrics}
    
    if (1-occluded.long()).sum() != 0:
        # compute metrics from tapvid
        samples = sample_queries_first(
            occluded.cpu().bool().numpy().squeeze(),
            gt_traj_2D.cpu().numpy().squeeze())
        tapvid_metrics = compute_tapvid_metrics(
            samples['query_points'],
            samples['occluded'],
            samples['target_points'],
            (1-pred_visibility).cpu().numpy(),
            gs_traj_2D.cpu().numpy(),
            W=w,
            H=h)
        metrics.update({'tapvid': tapvid_metrics})

    if vis_trajs:
        data['points'] = normalize_points(gs_traj_2D, h, w).squeeze()
        data['occluded'] = occluded.squeeze()
        data = {k: v.detach().clone().cpu().numpy() for k, v in data.items()}
        vis_tracked_points(
            results_dir,
            data)
    
    return metrics


def mask_valid_ids(valids, gs_traj_2D, gt_traj_2D, occluded, pred_visibility):
    # only keep points that are visible at time 0
    vis_ok = valids[:, :, 0] > 0

    # flatten along along batch * num points and mask
    shape = gs_traj_2D.shape
    vis_ok = vis_ok.reshape(shape[0]*shape[1])
    gs_traj_2D = gs_traj_2D.reshape(
        shape[0]*shape[1], shape[2], shape[3])[vis_ok].reshape(
            shape[0], -1, shape[2], shape[3])
    gt_traj_2D = gt_traj_2D.reshape(
        shape[0]*shape[1], shape[2], shape[3])[vis_ok].reshape(
            shape[0], -1, shape[2], shape[3])
    valids = valids.reshape(
        shape[0]*shape[1], shape[2])[vis_ok].reshape(
            shape[0], -1, shape[2])
    occluded = occluded.reshape(
        shape[0]*shape[1], shape[2])[vis_ok].reshape(
            shape[0], -1, shape[2])
    pred_visibility = pred_visibility.reshape(
        shape[0]*shape[1], shape[2])[vis_ok].reshape(
            shape[0], -1, shape[2])
    
    return gs_traj_2D, gt_traj_2D, valids, occluded, pred_visibility


def compute_metrics(
        H,
        W,
        gs_traj_2D,
        gt_traj_2D,
        valids,
        sur_thr=16,
        thrs=[1, 2, 4, 8, 16],
        norm_factor=256):
    B, N, S = gt_traj_2D.shape[0], gt_traj_2D.shape[1], gt_traj_2D.shape[2]
    
    # permute number of points and seq len
    gs_traj_2D = gs_traj_2D.permute(0, 2, 1, 3)
    gt_traj_2D = gt_traj_2D.permute(0, 2, 1, 3)
    valids = valids.permute(0, 2, 1)

    # get metrics
    metrics = dict()
    d_sum = 0.0
    sc_pt = torch.tensor(
        [[[W/norm_factor, H/norm_factor]]]).float().to(gs_traj_2D.device)
    for thr in thrs:
        # note we exclude timestep0 from this eval
        d_ = (torch.linalg.norm(
            gs_traj_2D[:,1:]/sc_pt - gt_traj_2D[:,1:]/sc_pt, dim=-1, ord=2) < thr).float() # B,S-1,N
        d_ = reduce_masked_mean(d_, valids[:,1:]).item()*100.0
        d_sum += d_
        metrics['d_%d' % thr] = d_
    d_avg = d_sum / len(thrs)
    metrics['d_avg'] = d_avg
    
    dists = torch.linalg.norm(gs_traj_2D/sc_pt - gt_traj_2D/sc_pt, dim=-1, ord=2) # B,S,N
    dist_ok = 1 - (dists > sur_thr).float() * valids # B,S,N
    survival = torch.cumprod(dist_ok, dim=1) # B,S,N
    metrics['survival'] = torch.mean(survival).item()*100.0

    # get the median l2 error for each trajectory
    dists_ = dists.permute(0,2,1).reshape(B*N,S)
    valids_ = valids.permute(0,2,1).reshape(B*N,S)
    median_l2 = reduce_masked_median(dists_, valids_, keep_batch=True)
    metrics['median_l2'] = median_l2.mean().item()

    return metrics


def sample_queries_first(
    target_occluded: np.ndarray,
    target_points: np.ndarray):
    """Package a set of frames and tracks for use in TAPNet evaluations.
    Given a set of frames and tracks with no query points, use the first
    visible point in each track as the query.
    Args:
      target_occluded: Boolean occlusion flag, of shape [n_tracks, n_frames],
        where True indicates occluded.
      target_points: Position, of shape [n_tracks, n_frames, 2], where each point
        is [x,y] scaled between 0 and 1.
      frames: Video tensor, of shape [n_frames, height, width, 3].  Scaled between
        -1 and 1.
    Returns:
      A dict with the keys:
        video: Video tensor of shape [1, n_frames, height, width, 3]
        query_points: Query points of shape [1, n_queries, 3] where
          each point is [t, y, x] scaled to the range [-1, 1]
        target_points: Target points of shape [1, n_queries, n_frames, 2] where
          each point is [x, y] scaled to the range [-1, 1]
    """

    valid = np.sum(~target_occluded, axis=1) > 0

    target_points = target_points[valid, :]
    target_occluded = target_occluded[valid, :]

    query_points = []
    for i in range(target_points.shape[0]):
        index = np.where(target_occluded[i] == 0)[0][0]
        x, y = target_points[i, index, 0], target_points[i, index, 1]
        query_points.append(np.array([index, y, x]))  # [t, y, x]
    query_points = np.stack(query_points, axis=0)
    return {
        "query_points": query_points[np.newaxis, ...],
        "target_points": target_points[np.newaxis, ...],
        "occluded": target_occluded[np.newaxis, ...],
    }


def compute_tapvid_metrics(
        query_points: np.ndarray,
        gt_occluded: np.ndarray,
        gt_tracks: np.ndarray,
        pred_occluded: np.ndarray,
        pred_tracks: np.ndarray,
        query_mode: str = 'first',
        norm_factor=256,
        W=256,
        H=256):
    """Computes TAP-Vid metrics (Jaccard, Pts. Within Thresh, Occ. Acc.)
    See the TAP-Vid paper for details on the metric computation.  All inputs are
    given in raster coordinates.  The first three arguments should be the direct
    outputs of the reader: the 'query_points', 'occluded', and 'target_points'.
    The paper metrics assume these are scaled relative to 256x256 images.
    pred_occluded and pred_tracks are your algorithm's predictions.
    This function takes a batch of inputs, and computes metrics separately for
    each video.  The metrics for the full benchmark are a simple mean of the
    metrics across the full set of videos.  These numbers are between 0 and 1,
    but the paper multiplies them by 100 to ease reading.
    Args:
       query_points: The query points, an in the format [t, y, x].  Its size is
         [b, n, 3], where b is the batch size and n is the number of queries
       gt_occluded: A boolean array of shape [b, n, t], where t is the number
         of frames.  True indicates that the point is occluded.
       gt_tracks: The target points, of shape [b, n, t, 2].  Each point is
         in the format [x, y]
       pred_occluded: A boolean array of predicted occlusions, in the same
         format as gt_occluded.
       pred_tracks: An array of track predictions from your algorithm, in the
         same format as gt_tracks.
       query_mode: Either 'first' or 'strided', depending on how queries are
         sampled.  If 'first', we assume the prior knowledge that all points
         before the query point are occluded, and these are removed from the
         evaluation.
    Returns:
        A dict with the following keys:
        occlusion_accuracy: Accuracy at predicting occlusion.
        pts_within_{x} for x in [1, 2, 4, 8, 16]: Fraction of points
          predicted to be within the given pixel threshold, ignoring occlusion
          prediction.
        jaccard_{x} for x in [1, 2, 4, 8, 16]: Jaccard metric for the given
          threshold
        average_pts_within_thresh: average across pts_within_{x}
        average_jaccard: average across jaccard_{x}
    """
    # SCALE to 256  
    sc_pt = np.array(
        [[[[W/norm_factor, H/norm_factor]]]])
    gt_tracks = gt_tracks/sc_pt
    pred_tracks = pred_tracks/sc_pt
    query_points[:, :, 1:] = query_points[:, :, 1:]/sc_pt[0]

    metrics = {}
    pred_occluded = gt_occluded

    # Don't evaluate the query point.  Numpy doesn't have one_hot, so we
    # replicate it by indexing into an identity matrix.
    one_hot_eye = np.eye(gt_tracks.shape[2])
    query_frame = query_points[..., 0]
    query_frame = np.round(query_frame).astype(np.int32)
    evaluation_points = one_hot_eye[query_frame] == 0

    # If we're using the first point on the track as a query, don't evaluate the
    # other points.
    if query_mode == "first":
        for i in range(gt_occluded.shape[0]):
            index = np.where(gt_occluded[i] == 0)[0][0]
            evaluation_points[i, :index] = False
    elif query_mode != "strided":
        raise ValueError("Unknown query mode " + query_mode)

    # Occlusion accuracy is simply how often the predicted occlusion equals the
    # ground truth.
    occ_acc = (
        np.sum(
            np.equal(pred_occluded, gt_occluded) & evaluation_points,
            axis=(1, 2),
        )
        / np.sum(evaluation_points)
    )
    metrics["occlusion_accuracy"] = occ_acc

    # Next, convert the predictions and ground truth positions into pixel
    # coordinates.
    visible = np.logical_not(gt_occluded)
    pred_visible = np.logical_not(pred_occluded)
    all_frac_within = []
    all_jaccard = []
    for thresh in [1, 2, 4, 8, 16]:
        # True positives are points that are within the threshold and where both
        # the prediction and the ground truth are listed as visible.
        within_dist = (
            np.sum(
                np.square(pred_tracks - gt_tracks),
                axis=-1,
            )
            < np.square(thresh)
        )
        is_correct = np.logical_and(within_dist, visible)

        # Compute the frac_within_threshold, which is the fraction of points
        # within the threshold among points that are visible in the ground truth,
        # ignoring whether they're predicted to be visible.
        count_correct = np.sum(
            is_correct & evaluation_points,
            axis=(1, 2),
        )
        count_visible_points = np.sum(visible & evaluation_points, axis=(1, 2))
        frac_correct = count_correct / count_visible_points
        metrics["pts_within_" + str(thresh)] = frac_correct
        all_frac_within.append(frac_correct)

        true_positives = np.sum(
            is_correct & pred_visible & evaluation_points, axis=(1, 2)
        )

        # The denominator of the jaccard metric is the true positives plus
        # false positives plus false negatives.  However, note that true positives
        # plus false negatives is simply the number of points in the ground truth
        # which is easier to compute than trying to compute all three quantities.
        # Thus we just add the number of points in the ground truth to the number
        # of false positives.
        #
        # False positives are simply points that are predicted to be visible,
        # but the ground truth is not visible or too far from the prediction.
        gt_positives = np.sum(visible & evaluation_points, axis=(1, 2))
        false_positives = (~visible) & pred_visible
        false_positives = false_positives | ((~within_dist) & pred_visible)
        false_positives = np.sum(false_positives & evaluation_points, axis=(1, 2))
        jaccard = true_positives / (gt_positives + false_positives)
        metrics["jaccard_" + str(thresh)] = jaccard
        all_jaccard.append(jaccard)
    metrics["average_jaccard"] = np.mean(
        np.stack(all_jaccard, axis=1),
        axis=1,
    )
    metrics["average_pts_within_thresh"] = np.mean(
        np.stack(all_frac_within, axis=1),
        axis=1,
    )
    return metrics


def reduce_masked_mean(x, mask, dim=None, keepdim=False):
    # x and mask are the same shape, or at least broadcastably so < actually it's safer if you disallow broadcasting
    # returns shape-1
    # axis can be a list of axes
    for (a,b) in zip(x.size(), mask.size()):
        # if not b==1: 
        assert(a==b) # some shape mismatch!
    # assert(x.size() == mask.size())
    prod = x*mask

    if dim is None:
        numer = torch.sum(prod)
        denom = 1e-10+torch.sum(mask)
    else:
        numer = torch.sum(prod, dim=dim, keepdim=keepdim)
        denom = 1e-10+torch.sum(mask, dim=dim, keepdim=keepdim)

    mean = numer/denom
    return mean


def reduce_masked_median(x, mask, keep_batch=False):
    # x and mask are the same shape
    assert(x.size() == mask.size())
    device = x.device

    B = list(x.shape)[0]
    x = x.detach().cpu().numpy()
    mask = mask.detach().cpu().numpy()
    if keep_batch:
        x = np.reshape(x, [B, -1])
        mask = np.reshape(mask, [B, -1])
        meds = np.zeros([B], np.float32)
        for b in list(range(B)):
            xb = x[b]
            mb = mask[b]
            if np.sum(mb) > 0:
                xb = xb[mb > 0]
                meds[b] = np.median(xb)
            else:
                meds[b] = np.nan
        meds = torch.from_numpy(meds).to(device)
        return meds.float()
    else:
        x = np.reshape(x, [-1])
        mask = np.reshape(mask, [-1])
        if np.sum(mask) > 0:
            x = x[mask > 0]
            med = np.median(x)
        else:
            med = np.nan
        med = np.array([med], np.float32)
        med = torch.from_numpy(med).to(device)
        return med.float()


def eval_traj(
        config,
        params=None,
        results_dir='out',
        cam=None,
        vis_trajs=True,
        gauss_ids_to_track=None,
        input_k=None,
        input_w2c=None, 
        load_gaussian_tracks=True,
        use_norm_pix=False,
        use_round_pix=False,
        use_depth_indicator=False,
        do_scale=False):
    # get projectoin matrix
    if cam is None:
        params, _, k, w2c = load_scene_data(config, results_dir)
        if k is None:
            k = input_k
            w2c = input_w2c
        h, w = config["data"]["desired_image_height"], config["data"]["desired_image_width"]
        proj_matrix = get_projection_matrix(w, h, k, w2c).squeeze()
        results_dir = os.path.join(results_dir, 'eval')

        if 'gauss_ids_to_track' in params.keys() and load_gaussian_tracks:
            gauss_ids_to_track = params['gauss_ids_to_track'].long()
            if gauss_ids_to_track.sum() == 0:
                gauss_ids_to_track = None
    else:
        proj_matrix = cam.projmatrix.squeeze()
        h = cam.image_height
        w = cam.image_width
        w2c = None

    # get gt data
    data = get_gt_traj(config, in_torch=True)
    if not 'jono' in config['data']["gradslam_data_cfg"]:
        dataset = 'davis'
    else:
        dataset = 'jono'

    # get metrics
    metrics = _eval_traj(
        params,
        params['timestep'],
        data,
        proj_matrix=proj_matrix,
        h=h,
        w=w,
        results_dir=os.path.join(results_dir, 'tracked_points_vis'),
        vis_trajs=vis_trajs,
        gauss_ids_to_track=gauss_ids_to_track,
        dataset=dataset,
        use_norm_pix=use_norm_pix,
        use_round_pix=use_round_pix,
        use_depth_indicator=use_depth_indicator,
        do_scale=do_scale,
        w2c=w2c)

    return metrics


def meshgrid2d(B, Y, X, stack=False, norm=False, device='cuda:0', on_chans=False):
    # returns a meshgrid sized B x Y x X

    grid_y = torch.linspace(0.0, Y-1, Y, device=torch.device(device))
    grid_y = torch.reshape(grid_y, [1, Y, 1])
    grid_y = grid_y.repeat(B, 1, X)

    grid_x = torch.linspace(0.0, X-1, X, device=torch.device(device))
    grid_x = torch.reshape(grid_x, [1, 1, X])
    grid_x = grid_x.repeat(B, Y, 1)

    if norm:
        grid_y, grid_x = normalize_grid2d(
            grid_y, grid_x, Y, X)

    if stack:
        # note we stack in xy order
        # (see https://pytorch.org/docs/stable/nn.functional.html#torch.nn.functional.grid_sample)
        if on_chans:
            grid = torch.stack([grid_x, grid_y], dim=1)
        else:
            grid = torch.stack([grid_x, grid_y], dim=-1)
        return grid
    else:
        return grid_y, grid_x


def get_xy_grid(H, W, N=1024, B=1, device='cuda:0'):
    # pick N points to track; we'll use a uniform grid
    N_ = np.sqrt(N).round().astype(np.int32)
    grid_y, grid_x = meshgrid2d(B, N_, N_, stack=False, norm=False, device=device)
    grid_y = 8 + grid_y.reshape(B, -1)/float(N_-1) * (H-16)
    grid_x = 8 + grid_x.reshape(B, -1)/float(N_-1) * (W-16)
    xy0 = torch.stack([grid_x, grid_y], dim=-1) # B, N_*N_, 2

    return xy0

def vis_grid_trajs(config, params=None, cam=None, results_dir=None, orig_image_size=False):
    # get projectoin matrix
    if cam is None:
        params, _, k, w2c = load_scene_data(config, results_dir)
        if orig_image_size:
            k, pose, h, w = get_cam_data(config, orig_image_size=orig_image_size)
            w2c = torch.linalg.inv(pose)
            proj_matrix = get_projection_matrix(w, h, k, w2c, device=params['means3D'].device).squeeze()
        else:
            h, w = config["data"]["desired_image_height"], config["data"]["desired_image_width"]
            proj_matrix = get_projection_matrix(w, h, k, w2c, device=params['means3D'].device).squeeze()
        results_dir = os.path.join(results_dir, 'eval')
    elif orig_image_size:
        k, pose, h, w = get_cam_data(config, orig_image_size=orig_image_size)
        w2c = torch.linalg.inv(pose)
        proj_matrix = get_projection_matrix(w, h, k, w2c, device=params['means3D'].device).squeeze()
    else:
        proj_matrix = cam.projmatrix.squeeze()
        h = cam.image_height
        w = cam.image_width

    # get trajectories to track
    start_pixels = get_xy_grid(h, w, device=params['means3D'].device).squeeze().long()
    gs_traj_2D, gs_traj_3D, gauss_ids, pred_visibility = get_gs_traj_pts(
        proj_matrix,
        params,
        params['timestep'],
        w,
        h,
        start_pixels,
        start_pixels_normalized=False,
        no_bg=False)

    # get gt data for visualization (actually only need rgb here)
    data = get_gt_traj(config, in_torch=True)

    data['points'] = normalize_points(gs_traj_2D, h, w).squeeze()
    data['occluded'] = torch.zeros(data['points'].shape[:-1]).to(data['points'].device)
    data = {k: v.detach().clone().cpu().numpy() for k, v in data.items()}
    vis_tracked_points(
        os.path.join(results_dir, 'grid_points_vis'),
        data)

