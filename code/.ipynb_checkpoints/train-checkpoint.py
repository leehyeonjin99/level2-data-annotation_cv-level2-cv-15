import os
import os.path as osp
import time
import math
import random
from datetime import timedelta
from argparse import ArgumentParser

import torch
from torch import cuda
from torch.utils.data import DataLoader
from torch.optim import lr_scheduler
from tqdm import tqdm
import numpy as np
import imgaug

from importlib import import_module
import madgrad
import adamp

from east_dataset import EASTDataset
from dataset import SceneTextDataset
from model import EAST
import wandb

import dataset_no_resize_no_crop

def parse_args():
    parser = ArgumentParser()

    # Conventional args
    parser.add_argument('--data_dir', type=str,
                        default=os.environ.get('SM_CHANNEL_TRAIN', '../input/data/ICDAR17_Korean'))
    parser.add_argument('--model_dir', type=str, default=os.environ.get('SM_MODEL_DIR',
                                                                        'trained_models'))
    parser.add_argument('--project', type=str, default = "data-annotation")
    parser.add_argument('--entity', type=str, default ="boostcampaitech3")
    parser.add_argument('--name', type=str, default ="Lim_contineous_learning")
    parser.add_argument('--device', default='cuda' if cuda.is_available() else 'cpu')
    parser.add_argument('--num_workers', type=int, default=4)

    parser.add_argument('--image_size', type=int, default=1024)
    parser.add_argument('--input_size', type=int, default=512)
    parser.add_argument('--batch_size', type=int, default=12)
    parser.add_argument('--learning_rate', type=float, default=1e-3)
    parser.add_argument('--max_epoch', type=int, default=200)
    parser.add_argument('--save_interval', type=int, default=5)

    ## Our argument
    parser.add_argument('--seed', type=int, default=2021)
    parser.add_argument('--optimizer', type=str, default = "Adam")
    parser.add_argument('--exp_name', type=str, default = "exp")
    
    #to load parameters
    parser.add_argument('--load', type=str, default = "F")
    
    
    args = parser.parse_args()

    if args.input_size % 32 != 0:
        raise ValueError('`input_size` must be a multiple of 32')

    return args

def set_seed(seed) :
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed) # if use multi-GPU
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    os.environ['PYTHONHASHSEED'] = str(seed)
    np.random.seed(seed)
    random.seed(seed)
    imgaug.random.seed(seed)
    print(f"seed : {seed}")

def do_training(data_dir, model_dir, device, image_size, input_size, num_workers, batch_size,
                learning_rate, max_epoch, save_interval, project, entity, name, seed, optimizer, exp_name, load):  
    
    wandb.init(project=project, entity=entity, name = name)
    wandb.config = {
        "learning_rate": learning_rate,
        "epoch": max_epoch,
        "batch_size": batch_size
    }

    set_seed(seed)

    def seed_worker(worker_id):
        worker_seed = torch.initial_seed() % 2**32
        np.random.seed(worker_seed)
        random.seed(worker_seed)

    dataset = SceneTextDataset(data_dir, split='train', image_size=image_size, crop_size=input_size)
   
    dataset = EASTDataset(dataset)
  
    num_batches = math.ceil(len(dataset) / batch_size)
    train_loader = DataLoader(dataset, batch_size=batch_size, shuffle=True, num_workers=num_workers, worker_init_fn=seed_worker)

    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    model = EAST()
    
    ################################?????? ???????????? ?????? #############################################
    if load != "F":
        if   load =="T": #####????????? 0.67 ?????? ???????????? ######
            print('load parameter from /baseline_200_(ICDAR17_all).pth')
            model.load_state_dict(torch.load(model_dir+"/baseline_200_(ICDAR17_all).pth"))

        elif load =="L": ##### ???????????? ?????? ????????????(exp_name ???????????? ??????)#####
            print('load parameter from latest.pth')
            model.load_state_dict(torch.load(model_dir+f'/{exp_name}_latest.pth'))
        else :           ##### pth ???????????? (???, ???????????? ?????? ??????????????? ???????????????) #####
            print(f'load parameter {load}')
            model.load_state_dict(torch.load(model_dir+'/'+load)) 
    #################################################################################################
    
    model.to(device)
    # optimizer
    if optimizer in dir(torch.optim):
        opt_module = getattr(import_module('torch.optim'), optimizer)
        optimizer = opt_module(model.parameters(), lr=learning_rate)
    elif optimizer == 'MADGRAD':
        optimizer = madgrad.MADGRAD(model.parameters(), lr=learning_rate)
    elif optimizer == 'AdamP':
        optimizer = adamp.AdamP(model.parameters(), lr=learning_rate)
    
    scheduler = lr_scheduler.MultiStepLR(optimizer, milestones=[max_epoch // 2], gamma=0.1)

    model.train()
    for epoch in range(max_epoch):
        epoch_loss, epoch_Cls, epoch_Angle, epoch_IoU, epoch_start = 0, 0, 0, 0, time.time()
        with tqdm(total=num_batches) as pbar:
            for img, gt_score_map, gt_geo_map, roi_mask in train_loader:
                pbar.set_description('[Epoch {}]'.format(epoch + 1))

                loss, extra_info = model.train_step(img, gt_score_map, gt_geo_map, roi_mask) 
                
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

                loss_val = loss.item()
                epoch_loss += loss_val
                epoch_Cls += extra_info['cls_loss']
                epoch_Angle += extra_info['angle_loss']
                epoch_IoU += extra_info['iou_loss']

                pbar.update(1)
                val_dict = {
                    'Cls loss': extra_info['cls_loss'], 'Angle loss': extra_info['angle_loss'],
                    'IoU loss': extra_info['iou_loss']
                }
                pbar.set_postfix(val_dict)
        
        
        scheduler.step()

        print('Mean loss: {:.4f} | Elapsed time: {}'.format(
            epoch_loss / num_batches, timedelta(seconds=time.time() - epoch_start)))

        wandb.log({ 'Mean Cls loss': epoch_Cls / num_batches, 
                    'Mean Angle loss': epoch_Angle / num_batches,
                    'Mean IoU loss': epoch_IoU / num_batches,
                    'Mean_loss': epoch_loss / num_batches})

        if (epoch + 1) % save_interval == 0:
            if not osp.exists(model_dir):
                os.makedirs(model_dir)

            ckpt_fpath = osp.join(model_dir, f'{exp_name}_latest.pth')
            torch.save(model.state_dict(), ckpt_fpath)


def main(args):
    do_training(**args.__dict__)


if __name__ == '__main__':
    args = parse_args()
    main(args)
