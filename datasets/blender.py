import os
import json
import math
import numpy as np
from PIL import Image

import torch
from torch.utils.data import Dataset, DataLoader, IterableDataset
import torchvision.transforms.functional as TF

import pytorch_lightning as pl

import datasets
from models.ray_utils import get_ray_directions
from utils.misc import get_rank


class BlenderDatasetBase():
    def setup(self, config, split):
        self.config = config
        self.split = split
        self.rank = get_rank()

        self.has_mask = True
        self.apply_mask = self.has_mask and self.config.apply_mask

        with open(os.path.join(self.config.root_dir, f"transforms_{self.split}.json"), 'r') as f:
            meta = json.load(f)

        if 'w' in meta and 'h' in meta:
            W, H = int(meta['w']), int(meta['h'])
        else:
            W, H = 800, 800

        if 'img_wh' in self.config:
            w, h = self.config.img_wh
            assert round(W / w * h) == H
        elif 'img_downscale' in self.config:
            w, h = W // self.config.img_downscale, H // self.config.img_downscale
        else:
            raise KeyError("Either img_wh or img_downscale should be specified.")
        
        self.w, self.h = w, h
        self.img_wh = (self.w, self.h)

        self.near, self.far = self.config.near_plane, self.config.far_plane

        try:
            self.focal_x = meta['fl_x']
            self.focal_y = meta['fl_y']
            self.cx = meta['cx']
            self.cy = meta['cy']
            self.k1 = meta['k1']
            self.k2 = meta['k2']
            c3vd_data = True
        except:
            self.focal_x = 0.5 * w / math.tan(0.5 * meta['camera_angle_x']) # scaled focal length
            self.focal_y = self.focal_x
            self.cx = self.w//2
            self.cy = self.h//2
            self.k1 = 0.0
            self.k2 = 0.0
            c3vd_data = False

        # ray directions for all pixels, same for all images (same H, W, focal)
        self.directions = \
            get_ray_directions(self.w, self.h, self.focal_x, self.focal_y, self.cx, self.cy, k1=self.k1, k2=self.k2).to(self.rank) # (h, w, 3)           

        self.all_c2w, self.all_images, self.all_fg_masks = [], [], []

        for i, frame in enumerate(meta['frames']):
            c2w = torch.from_numpy(np.array(frame['transform_matrix'])[:3, :4])
            if c3vd_data:
                c2w[:3,1:3] *= -1. # OpenGL or COLMAP coordinates
            self.all_c2w.append(c2w)

            img_path = os.path.join(self.config.root_dir, f"{frame['file_path']}.png")
            img = Image.open(img_path)
            img = img.resize(self.img_wh, Image.BICUBIC)
            img = TF.to_tensor(img).permute(1, 2, 0) # (4, h, w) => (h, w, 4)

            if self.apply_mask:
                if c3vd_data:
                    depth_path = img_path.replace("images", "depths").replace("color.png", "depth.tiff")
                    depth = Image.open(depth_path).convert('L')
                    depth = depth.resize(self.img_wh, Image.BICUBIC)
                    depth = TF.to_tensor(depth).permute(1, 2, 0) # (4, h, w) => (h, w, 4)
                    mask = torch.ones_like(img[...,0], device=img.device)
                    mask[depth[...,0] == 0] = 0.0
                    self.all_fg_masks.append(mask) # (h, w)
                else:
                    self.all_fg_masks.append(img[..., -1]) # (h, w)
            else:
                self.all_fg_masks.append(torch.ones_like(img[...,0], device=img.device)) # (h, w)
            self.all_images.append(img[...,:3])

        self.all_c2w, self.all_images, self.all_fg_masks = \
            torch.stack(self.all_c2w, dim=0).float().to(self.rank), \
            torch.stack(self.all_images, dim=0).float().to(self.rank), \
            torch.stack(self.all_fg_masks, dim=0).float().to(self.rank)

        # translate
        # self.all_c2w[...,3] -= self.all_c2w[...,3].mean(0)

        # rescale
        if 'cam_downscale' not in self.config:
            scale = 1.0
        elif self.config.cam_downscale:
            scale = self.config.cam_downscale
        else:
            # auto-scale with camera positions
            scale = self.all_c2w[...,3].norm(p=2, dim=-1).min()
            print('auto-scaled by: ', scale)
        self.all_c2w[...,3] /= scale
        

class BlenderDataset(Dataset, BlenderDatasetBase):
    def __init__(self, config, split):
        self.setup(config, split)

    def __len__(self):
        return len(self.all_images)
    
    def __getitem__(self, index):
        return {
            'index': index
        }


class BlenderIterableDataset(IterableDataset, BlenderDatasetBase):
    def __init__(self, config, split):
        self.setup(config, split)

    def __iter__(self):
        while True:
            yield {}


@datasets.register('blender')
class BlenderDataModule(pl.LightningDataModule):
    def __init__(self, config):
        super().__init__()
        self.config = config
    
    def setup(self, stage=None):
        if stage in [None, 'fit']:
            self.train_dataset = BlenderIterableDataset(self.config, self.config.train_split)
        if stage in [None, 'fit', 'validate']:
            self.val_dataset = BlenderDataset(self.config, self.config.val_split)
        if stage in [None, 'test']:
            self.test_dataset = BlenderDataset(self.config, self.config.test_split)
        if stage in [None, 'predict']:
            self.predict_dataset = BlenderDataset(self.config, self.config.train_split)

    def prepare_data(self):
        pass
    
    def general_loader(self, dataset, batch_size):
        sampler = None
        return DataLoader(
            dataset, 
            num_workers=os.cpu_count(), 
            batch_size=batch_size,
            pin_memory=True,
            sampler=sampler
        )
    
    def train_dataloader(self):
        return self.general_loader(self.train_dataset, batch_size=1)

    def val_dataloader(self):
        return self.general_loader(self.val_dataset, batch_size=1)

    def test_dataloader(self):
        return self.general_loader(self.test_dataset, batch_size=1) 

    def predict_dataloader(self):
        return self.general_loader(self.predict_dataset, batch_size=1)       
