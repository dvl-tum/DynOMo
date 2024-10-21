# MIT License

# Copyright (c) 2022 Intelligent Systems Lab Org

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# File author: Shariq Farooq Bhat

import argparse
from pprint import pprint

import torch
from zoedepth.utils.easydict import EasyDict as edict
from tqdm import tqdm

from zoedepth.data.data_mono import DepthDataLoader
from zoedepth.models.builder import build_model
from zoedepth.utils.arg_utils import parse_unknown
from zoedepth.utils.config import change_dataset, get_config, ALL_EVAL_DATASETS, ALL_INDOOR, ALL_OUTDOOR
from zoedepth.utils.misc import (RunningAverageDict, colors, compute_metrics,
                        count_parameters)
import numpy as np
import os



@torch.no_grad()
def infer(model, images, do_mean_pred=False, **kwargs):
    """Inference with flip augmentation"""
    # images.shape = N, C, H, W
    def get_depth_from_prediction(pred):
        if isinstance(pred, torch.Tensor):
            pred = pred  # pass
        elif isinstance(pred, (list, tuple)):
            pred = pred[-1]
        elif isinstance(pred, dict):
            pred = pred['metric_depth'] if 'metric_depth' in pred else pred['out']
        else:
            raise NotImplementedError(f"Unknown output type {type(pred)}")
        return pred

    pred1 = model(images, **kwargs)
    pred1 = get_depth_from_prediction(pred1)
    
    if do_mean_pred:
        pred2 = model(torch.flip(images, [3]), **kwargs)
        pred2 = get_depth_from_prediction(pred2)
        pred2 = torch.flip(pred2, [3])

        mean_pred = 0.5 * (pred1 + pred2)
        return mean_pred
    else:
        return pred1


@torch.no_grad()
def evaluate(model, test_loader, config, round_vals=True, round_precision=3, device="cuda:4"):
    model.eval()
    metrics = RunningAverageDict()
    print('Starting evaluation...')
    save_dir = test_loader.dataset.save_dir
    os.makedirs(save_dir, exist_ok=True)
    for i, sample in tqdm(enumerate(test_loader), total=len(test_loader)):
        if test_loader.dataset.name == 'DAVIS' or test_loader.dataset.name == 'iphone':
            save_name = "depth" + os.path.basename(test_loader.dataset.image_files[i])[:-3] + 'npy'
        elif test_loader.dataset.name == 'rgb':
            save_name = test_loader.dataset.image_files[i]
        else:
            save_name = "depth" + os.path.basename(test_loader.dataset.image_files[i])[1:-3] + 'npy'

        if 'has_valid_depth' in sample:
            if not sample['has_valid_depth']:
                continue
        image, depth = sample['image'], sample['depth']
        image, depth = image.to(device), depth.to(device)
        depth = depth.squeeze().unsqueeze(0).unsqueeze(0)
        focal = sample.get('focal', torch.Tensor(
            [715.0873]).to(device))  # This magic number (focal) is only used for evaluating BTS model

        pred = infer(model, image, dataset=sample['dataset'][0], focal=focal)

        # Save image, depth, pred for visualization
        if "save_images" in config and config.save_images:
            # print("Saving images ...")
            from PIL import Image
            import torchvision.transforms as transforms
            from zoedepth.utils.misc import colorize

            # os.makedirs(config.save_images, exist_ok=True)
            # def save_image(img, path):
            # d = colorize(depth.squeeze().cpu().numpy(), 0, 10)
            # p = colorize(pred.squeeze().cpu().numpy(), 0, 10)

            # im = transforms.ToPILImage()(image.squeeze().cpu())
            # im.save(os.path.join(config.save_images, "frame{:0>5}.png").format(i))
            # colorized
            # Image.fromarray(p).save(os.path.join(config.save_images, "DCMAP{:0>5}.png").format(i))
            
            os.makedirs(os.path.dirname(os.path.join(save_dir, save_name)), exist_ok=True)
            np.save(os.path.join(save_dir, save_name).format(i), pred.squeeze().unsqueeze(2).cpu().numpy())
            print(os.path.join(save_dir, save_name).format(i))
            
        # print(depth.shape, pred.shape)
        metrics.update(compute_metrics(depth, pred, config=config))

    if round_vals:
        def r(m): return round(m, round_precision)
    else:
        def r(m): return m
    metrics = {k: r(v) for k, v in metrics.get_value().items()}
    return metrics

def main(config, device):
    model = build_model(config)
    test_loader = DepthDataLoader(config, 'online_eval').data
    model = model.to(device)
    metrics = evaluate(model, test_loader, config, device)
    print(f"{colors.fg.green}")
    print(metrics)
    print(f"{colors.reset}")
    metrics['#params'] = f"{round(count_parameters(model, include_all=True)/1e6, 2)}M"
    return metrics


def eval_model(model_name, pretrained_resource, dataset='nyu', device="cuda:4", **kwargs):

    # Load default pretrained resource defined in config if not set
    overwrite = {**kwargs, "pretrained_resource": pretrained_resource} if pretrained_resource else kwargs
    config = get_config(model_name, "eval", dataset, **overwrite)
    # config = change_dataset(config, dataset)  # change the dataset

    print(f"Evaluating {model_name} on {dataset}...")
    metrics = main(config, device)
    return metrics


if __name__ == '__main__':
    device = "cuda:4"
    parser = argparse.ArgumentParser()
    parser.add_argument("-m", "--model", type=str,
                        required=True, help="Name of the model to evaluate")
    parser.add_argument("-p", "--pretrained_resource", type=str,
                        required=False, default="", help="Pretrained resource to use for fetching weights. If not set, default resource from model config is used,  Refer models.model_io.load_state_from_resource for more details.")
    parser.add_argument("-d", "--dataset", type=str, required=False,
                        default='nyu', help="Dataset to evaluate on")

    args, unknown_args = parser.parse_known_args()
    overwrite_kwargs = parse_unknown(unknown_args)

    if "ALL_INDOOR" in args.dataset:
        datasets = ALL_INDOOR
    elif "ALL_OUTDOOR" in args.dataset:
        datasets = ALL_OUTDOOR
    elif "ALL" in args.dataset:
        datasets = ALL_EVAL_DATASETS
    elif "," in args.dataset:
        datasets = args.dataset.split(",")
    else:
        datasets = [args.dataset]
    
    for dataset in datasets:
        eval_model(args.model, pretrained_resource=args.pretrained_resource,
                    dataset=dataset, device=device, **overwrite_kwargs)
