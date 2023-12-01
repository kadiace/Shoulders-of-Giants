# Copyright (c) 2020-2022 NVIDIA CORPORATION & AFFILIATES. All rights reserved. 
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction, 
# disclosure or distribution of this material and related documentation 
# without an express license agreement from NVIDIA CORPORATION or 
# its affiliates is strictly prohibited.

import os
import time
import argparse
import json
import numpy as np
import torch
import nvdiffrast.torch as dr

# Import data readers / generators
from dataset.dataset_mesh import DatasetMesh

# Import topology / geometry trainers
from geometry.dlmesh import DLMesh
from render import obj, material, util, mesh, texture, mlptexture, light, render, util
from sd import StableDiffusion
from tqdm import tqdm
import random
import imageio


###############################################################################
# Mix background into a dataset image
###############################################################################
@torch.no_grad()
def prepare_batch(target, background= 'black',it = 0,coarse_iter=0):
    target['mv'] = target['mv'].cuda()
    target['mvp'] = target['mvp'].cuda()
    target['campos'] = target['campos'].cuda()
    target['normal_rotate'] = target['normal_rotate'].cuda()
    batch_size = target['mv'].shape[0]
    resolution = target['resolution']
    if background == 'white':
        target['background']= torch.ones(batch_size, resolution[0], resolution[1], 3, dtype=torch.float32, device='cuda') 
    if background == 'black':
        target['background'] = torch.zeros(batch_size, resolution[0], resolution[1], 3, dtype=torch.float32, device='cuda')
    return target

###############################################################################
# UV - map geometry & convert to a mesh
###############################################################################

@torch.no_grad()
def xatlas_uvmap(glctx, geometry, mat, FLAGS):
    eval_mesh = geometry.getMesh(mat)
    new_mesh = mesh.Mesh( base=eval_mesh)
    mask, kd, ks, normal = render.render_uv(glctx, new_mesh, FLAGS.texture_res, eval_mesh.material['kd_ks_normal'], FLAGS.uv_padding_block)
    
    if FLAGS.layers > 1:
        kd = torch.cat((kd, torch.rand_like(kd[...,0:1])), dim=-1)

    kd_min, kd_max = torch.tensor(FLAGS.kd_min, dtype=torch.float32, device='cuda'), torch.tensor(FLAGS.kd_max, dtype=torch.float32, device='cuda')
    ks_min, ks_max = torch.tensor(FLAGS.ks_min, dtype=torch.float32, device='cuda'), torch.tensor(FLAGS.ks_max, dtype=torch.float32, device='cuda')
    nrm_min, nrm_max = torch.tensor(FLAGS.nrm_min, dtype=torch.float32, device='cuda'), torch.tensor(FLAGS.nrm_max, dtype=torch.float32, device='cuda')

    new_mesh.material = material.Material({
        'bsdf'   : mat['bsdf'],
        'kd'     : texture.Texture2D(kd, min_max=[kd_min, kd_max]),
        'ks'     : texture.Texture2D(ks, min_max=[ks_min, ks_max]),
        'normal' : texture.Texture2D(normal, min_max=[nrm_min, nrm_max])
    })

    return new_mesh

###############################################################################
# Utility functions for material
##############################################################################

def init_material(geometry, FLAGS):
    kd_min, kd_max = torch.tensor(FLAGS.kd_min, dtype=torch.float32, device='cuda'), torch.tensor(FLAGS.kd_max, dtype=torch.float32, device='cuda')
    ks_min, ks_max = torch.tensor(FLAGS.ks_min, dtype=torch.float32, device='cuda'), torch.tensor(FLAGS.ks_max, dtype=torch.float32, device='cuda')
    nrm_min, nrm_max = torch.tensor(FLAGS.nrm_min, dtype=torch.float32, device='cuda'), torch.tensor(FLAGS.nrm_max, dtype=torch.float32, device='cuda')

    mlp_min = torch.cat((kd_min[0:3], ks_min, nrm_min), dim=0)
    mlp_max = torch.cat((kd_max[0:3], ks_max, nrm_max), dim=0)
    mlp_map_opt = mlptexture.MLPTexture3D(geometry.getAABB(), channels=9, min_max=[mlp_min, mlp_max])
    mat =  material.Material({'kd_ks_normal' : mlp_map_opt})

    mat['bsdf'] = 'pbr'

    return mat

###############################################################################
# Validation & testing
###############################################################################
def validate_itr(glctx, target, geometry, opt_material, lgt, FLAGS, relight = None):
    result_dict = {}
    with torch.no_grad():
        with torch.no_grad():
            lgt.build_mips()
            if FLAGS.camera_space_light:
                lgt.xfm(target['mv'])
            if relight != None:
                relight.build_mips()
        buffers = geometry.render(glctx, target, lgt, opt_material, if_use_bump = FLAGS.if_use_bump)
        result_dict['shaded'] =  buffers['shaded'][0, ..., 0:3]
        result_dict['shaded'] = util.rgb_to_srgb(result_dict['shaded'])
        if relight != None:
            result_dict['relight'] = geometry.render(glctx, target, relight, opt_material, if_use_bump = FLAGS.if_use_bump)['shaded'][0, ..., 0:3]
            result_dict['relight'] = util.rgb_to_srgb(result_dict['relight'])
        result_dict['mask'] = (buffers['shaded'][0, ..., 3:4])
        result_image = result_dict['shaded']

        if FLAGS.display is not None :
            for layer in FLAGS.display:
                if 'latlong' in layer and layer['latlong']:
                    if isinstance(lgt, light.EnvironmentLight):
                        result_dict['light_image'] = util.cubemap_to_latlong(lgt.base, FLAGS.display_res)
                    result_image = torch.cat([result_image, result_dict['light_image']], axis=1)
                elif 'bsdf' in layer:
                    buffers  = geometry.render(glctx, target, lgt, opt_material, bsdf=layer['bsdf'],  if_use_bump = FLAGS.if_use_bump)
                    if layer['bsdf'] == 'kd':
                        result_dict[layer['bsdf']] = util.rgb_to_srgb(buffers['shaded'][0, ..., 0:3])  
                    elif layer['bsdf'] == 'normal':
                        result_dict[layer['bsdf']] = (buffers['shaded'][0, ..., 0:3] + 1) * 0.5
                    else:
                        result_dict[layer['bsdf']] = buffers['shaded'][0, ..., 0:3]
                    result_image = torch.cat([result_image, result_dict[layer['bsdf']]], axis=1)

        return result_image, result_dict

def save_gif(dir,fps):
    imgpath = dir
    frames = []
    for idx in sorted(os.listdir(imgpath)):
        # print(idx)
        img = os.path.join(imgpath,idx)
        frames.append(imageio.imread(img))
    imageio.mimsave(os.path.join(dir, 'eval.gif'),frames,'GIF',duration=1/fps)
    
@torch.no_grad()     
def validate(glctx, geometry, opt_material, lgt, dataset_validate, out_dir, FLAGS, relight= None):

    # ==============================================================================================
    #  Validation loop
    # ==============================================================================================

    dataloader_validate = torch.utils.data.DataLoader(dataset_validate, batch_size=1, collate_fn=dataset_validate.collate)

    os.makedirs(out_dir, exist_ok=True)
    
    shaded_dir = os.path.join(out_dir, "shaded")
    relight_dir = os.path.join(out_dir, "relight")
    kd_dir = os.path.join(out_dir, "kd")
    ks_dir = os.path.join(out_dir, "ks")
    normal_dir = os.path.join(out_dir, "normal")
    mask_dir = os.path.join(out_dir, "mask")
    
    os.makedirs(shaded_dir, exist_ok=True)
    os.makedirs(relight_dir, exist_ok=True)
    os.makedirs(kd_dir, exist_ok=True)
    os.makedirs(ks_dir, exist_ok=True)
    os.makedirs(normal_dir, exist_ok=True)
    os.makedirs(mask_dir, exist_ok=True)
    
    print("Running validation")
    dataloader_validate = tqdm(dataloader_validate)
    for it, target in enumerate(dataloader_validate):

        # Mix validation background
        target = prepare_batch(target, 'white')

        result_image, result_dict = validate_itr(glctx, target, geometry, opt_material, lgt, FLAGS, relight)
        for k in result_dict.keys():
            np_img = result_dict[k].detach().cpu().numpy()
            if k == 'shaded':
                util.save_image(shaded_dir + '/' + ('val_%06d_%s.png' % (it, k)), np_img)
            elif k == 'relight':
                util.save_image(relight_dir + '/' + ('val_%06d_%s.png' % (it, k)), np_img)
            elif k == 'kd':
                util.save_image(kd_dir + '/' + ('val_%06d_%s.png' % (it, k)), np_img)
            elif k == 'ks':
                util.save_image(ks_dir + '/' + ('val_%06d_%s.png' % (it, k)), np_img)
            elif k == 'normal':
                util.save_image(normal_dir + '/' + ('val_%06d_%s.png' % (it, k)), np_img)
            elif k == 'mask':
                util.save_image(mask_dir + '/' + ('val_%06d_%s.png' % (it, k)), np_img)
    if 'shaded' in result_dict.keys():
        save_gif(shaded_dir,30)
    if 'relight' in result_dict.keys():
        save_gif(relight_dir,30)
    if 'kd' in result_dict.keys():
        save_gif(kd_dir,30)
    if 'ks' in result_dict.keys():
        save_gif(ks_dir,30)
    if 'normal' in result_dict.keys():
        save_gif(normal_dir,30)
    if 'mask' in result_dict.keys():
        save_gif(mask_dir,30)
    return 0

###############################################################################
# Main shape fitter function / optimization loop
###############################################################################

class Trainer(torch.nn.Module):
    def __init__(self, glctx, geometry, lgt, mat, optimize_geometry, optimize_light, FLAGS, stable_diffusion):
        super(Trainer, self).__init__()

        self.glctx = glctx
        self.geometry = geometry
        self.light = lgt
        self.material = mat
        self.optimize_geometry = optimize_geometry
        self.optimize_light = optimize_light
        self.FLAGS = FLAGS
        self.stable_diffusion = stable_diffusion
        self.if_flip_the_normal = FLAGS.if_flip_the_normal
        self.if_use_bump = FLAGS.if_use_bump
        if not self.optimize_light:
            with torch.no_grad():
                self.light.build_mips()

        self.params = list(self.material.parameters())
        self.params += list(self.light.parameters()) if optimize_light else []
        self.geo_params = list(self.geometry.parameters()) if optimize_geometry else []
      

    def forward(self, target, it, if_normal, if_pretrain, scene_and_vertices ):
        if self.optimize_light:
            self.light.build_mips()
            if self.FLAGS.camera_space_light:
                self.light.xfm(target['mv'])
        if if_pretrain:        
            return self.geometry.decoder.pre_train_ellipsoid(it, scene_and_vertices)
        else:
            return self.geometry.tick(glctx, target, self.light, self.material, it , if_normal, self.stable_diffusion, self.if_flip_the_normal, self.if_use_bump)

def optimize_mesh(
    glctx,
    geometry,
    opt_material,
    lgt,
    dataset_train,
    dataset_validate,
    FLAGS,
    optimize_light=True,
    optimize_geometry=True,
    stable_diffusion = None,
    scene_and_vertices = None,
    ):
    
    dataloader_train    = torch.utils.data.DataLoader(dataset_train, batch_size=FLAGS.batch, collate_fn=dataset_train.collate, shuffle=False)
    dataloader_validate = torch.utils.data.DataLoader(dataset_validate, batch_size=1, collate_fn=dataset_train.collate)
   
    model = Trainer(glctx, geometry, lgt, opt_material, optimize_geometry, optimize_light, FLAGS, stable_diffusion)
    if optimize_geometry: 
        optimizer_mesh = torch.optim.AdamW(model.geo_params, lr=0.001, betas=(0.9, 0.99), eps=1e-15)
    optimizer = torch.optim.AdamW(model.params, lr=0.01, betas=(0.9, 0.99), eps=1e-15)
    if FLAGS.multi_gpu: 
        model = model.cuda()
        model = torch.nn.parallel.DistributedDataParallel(model,
                                                        device_ids=[FLAGS.local_rank],
                                                        find_unused_parameters= (False)
                                                        )
        
    img_cnt = 0
    img_loss_vec = []
    reg_loss_vec = []
    iter_dur_vec = []
   

    def cycle(iterable):
        iterator = iter(iterable)
        while True:
            try:
                yield next(iterator)
            except StopIteration:
                iterator = iter(iterable)

    v_it = cycle(dataloader_validate)
    scaler = torch.cuda.amp.GradScaler(enabled=True)  

    if FLAGS.local_rank == 0:
        dataloader_train = tqdm(dataloader_train)
    for it, target in enumerate(dataloader_train):

        # Mix randomized background into dataset image
        target = prepare_batch(target, FLAGS.train_background, it, FLAGS.coarse_iter)  

        # ==============================================================================================
        #  Display / save outputs. Do it before training so we get initial meshes
        # ==============================================================================================

        # Show/save image before training step (want to get correct rendering of input)
        if FLAGS.local_rank == 0:
            save_image = FLAGS.save_interval and (it % FLAGS.save_interval == 0)
            if  save_image:
                result_image, result_dict = validate_itr(glctx, prepare_batch(next(v_it), FLAGS.train_background), geometry, opt_material, lgt, FLAGS)  #prepare_batch(next(v_it), FLAGS.background)
                np_result_image = result_image.detach().cpu().numpy()
                util.save_image(FLAGS.out_dir + '/' + ('img_%s_%06d.png' % ("appearance_modeling", img_cnt)), np_result_image)
                img_cnt = img_cnt+1
                    
        iter_start_time = time.time()
        if_pretrain = False
        if_normal = False
        
        with torch.cuda.amp.autocast(enabled= True):
            if if_pretrain== True:
                reg_loss = model(target, it, if_normal, if_pretrain= if_pretrain, scene_and_vertices = scene_and_vertices)
                img_loss = 0 
                sds_loss = 0 
            if if_pretrain == False:
                sds_loss,img_loss, reg_loss = model(target, it, if_normal, if_pretrain= if_pretrain, scene_and_vertices =None)
    
        # ==============================================================================================
        #  Final loss
        # ==============================================================================================
        
        total_loss = img_loss + reg_loss + sds_loss
        
        # model.geometry.decoder.net.params.grad /= 100
        if if_pretrain == True:
            scaler.scale(total_loss).backward()
            
        if if_pretrain == False:
            scaler.scale(total_loss).backward()
            img_loss_vec.append(img_loss.item())

        reg_loss_vec.append(reg_loss.item())

        # ==============================================================================================
        #  Backpropagate
        # ==============================================================================================

        if if_normal == False and  if_pretrain == False:
            scaler.step(optimizer)
            optimizer.zero_grad()
          
        if if_normal == True or if_pretrain == True:
            if optimize_geometry:
                scaler.step(optimizer_mesh)
                optimizer_mesh.zero_grad()
                

        scaler.update()
        # ==============================================================================================
        #  Clamp trainables to reasonable range
        # ==============================================================================================
        with torch.no_grad():
            if 'kd' in opt_material:
                opt_material['kd'].clamp_()
            if 'ks' in opt_material:
                opt_material['ks'].clamp_()
            if 'normal' in opt_material:
                opt_material['normal'].clamp_()
                opt_material['normal'].normalize_()
            if lgt is not None:
                lgt.clamp_(min=0.0)

        torch.cuda.current_stream().synchronize()
        iter_dur_vec.append(time.time() - iter_start_time)

        # ==============================================================================================
        #  Logging
        # ==============================================================================================
        # log_interval = 10
        # if it % log_interval == 0 and FLAGS.local_rank == 0 and if_pretrain == False:
        #     img_loss_avg = np.mean(np.asarray(img_loss_vec[-log_interval:]))
        #     reg_loss_avg = np.mean(np.asarray(reg_loss_vec[-log_interval:]))
        #     iter_dur_avg = np.mean(np.asarray(iter_dur_vec[-log_interval:]))
            
        #     remaining_time = (FLAGS.iter-it)*iter_dur_avg
        #     if optimize_geometry:
        #         print("iter=%5d, img_loss=%.6f, reg_loss=%.6f, mesh_lr=%.5f, time=%.1f ms, rem=%s, mat_lr=%.5f" % 
        #             (it, img_loss_avg, reg_loss_avg, optimizer_mesh.param_groups[0]['lr'], iter_dur_avg*1000, util.time_to_text(remaining_time),optimizer.param_groups[0]['lr']))
        #     else:
        #         print("iter=%5d, img_loss=%.6f, reg_loss=%.6f, time=%.1f ms, rem=%s, mat_lr=%.5f" % 
        #             (it, img_loss_avg, reg_loss_avg, iter_dur_avg*1000, util.time_to_text(remaining_time),optimizer.param_groups[0]['lr']))
    return geometry, opt_material

def seed_everything(seed, local_rank):
    random.seed(seed + local_rank)
    os.environ['PYTHONHASHSEED'] = str(seed)
    np.random.seed(seed + local_rank)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    # torch.backends.cudnn.benchmark = True  

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='nvdiffrec')
    parser.add_argument('--config', type=str, default=None, help='Config file')
    parser.add_argument('-i', '--iter', type=int, default=5000)
    parser.add_argument('-b', '--batch', type=int, default=1)
    parser.add_argument('-s', '--spp', type=int, default=1)
    parser.add_argument('-l', '--layers', type=int, default=1)
    parser.add_argument('-r', '--train-res', nargs=2, type=int, default=[512, 512])
    parser.add_argument('-dr', '--display-res', type=int, default=None)
    parser.add_argument('-tr', '--texture-res', nargs=2, type=int, default=[1024, 1024])
    parser.add_argument('-si', '--save-interval', type=int, default=1000, help="The interval of saving an image")
    parser.add_argument('-mr', '--min-roughness', type=float, default=0.08)
    parser.add_argument('-mip', '--custom-mip', action='store_true', default=False)
    parser.add_argument('-rt', '--random-textures', action='store_true', default=False)
    parser.add_argument('-bg', '--train_background', default='black', choices=['black', 'white', 'checker', 'reference'])
    parser.add_argument('-o', '--out-dir', type=str, default=None)
    parser.add_argument('-rm', '--ref_mesh', type=str)
    parser.add_argument('-bm', '--base-mesh', type=str, default=None)
    parser.add_argument('--validate', type=bool, default=True)
    parser.add_argument("--local_rank", type=int, default=0, help="For distributed training: local_rank")
    parser.add_argument("--seed", type=int, default=42, help="A seed for reproducible training.")
    parser.add_argument("--add_directional_text", action='store_true', default=False)
    parser.add_argument('--mode', default='geometry_modeling', choices=['geometry_modeling', 'appearance_modeling'])
    parser.add_argument('--text', type=str, default="", help="text prompt")
    parser.add_argument('--sdf_init_shape', default='ellipsoid', choices=['ellipsoid', 'cylinder', 'custom_mesh'])
    parser.add_argument('--camera_random_jitter', type= float, default=0.4, help="A large value is advantageous for the extension of objects such as ears or sharp corners to grow.")
    parser.add_argument('--fovy_range', nargs=2, type=float, default=[25.71, 45.00])
    parser.add_argument('--elevation_range', nargs=2, type=int, default=[-10, 45], help="The elevatioin range must in [-90, 90].")
    parser.add_argument("--stable_diffusion_weight", type=int, default=100, help="The weight of classifier-free stable_diffusion")
    parser.add_argument("--sds_weight_strategy", type=int, nargs=1, default=0, choices=[0, 1, 2], help="The strategy of the sds loss's weight")
    parser.add_argument("--translation_y", type= float, nargs=1, default= 0 , help="translation of the initial shape on the y-axis")
    parser.add_argument("--translation_z", type= float, nargs=1, default= 0 , help="translation of the initial shape on the z-axis")
    parser.add_argument("--coarse_iter", type= int, nargs=1, default= 1000 , help="The iteration number of the coarse stage.")
    parser.add_argument('--early_time_step_range', nargs=2, type=float, default=[0.02, 0.5], help="The time step range in early phase")
    parser.add_argument('--late_time_step_range', nargs=2, type=float, default=[0.02, 0.5], help="The time step range in late phase")
    parser.add_argument("--sdf_init_shape_rotate_x", type= int, nargs=1, default= 0 , help="rotation of the initial shape on the x-axis")
    parser.add_argument("--if_flip_the_normal", action='store_true', default=False , help="Flip the x-axis positive half-axis of Normal. We find this process helps to alleviate the Janus problem.")
    parser.add_argument("--front_threshold", type= int, nargs=1, default= 45 , help="the range of front view would be [-front_threshold, front_threshold")
    parser.add_argument("--if_use_bump", type=bool, default= True , help="whether to use perturbed normals during appearing modeling")
    parser.add_argument("--uv_padding_block", type= int, default= 4 , help="The block of uv padding.")
    parser.add_argument("--negative_text", type=str, default="", help="adding negative text can improve the visual quality in appearance modeling")
    FLAGS = parser.parse_args()
    FLAGS.mtl_override        = None                     # Override material of model
    FLAGS.dmtet_grid          = 64                       # Resolution of initial tet grid. We provide 64, 128 and 256 resolution grids. Other resolutions can be generated with https://github.com/crawforddoran/quartet
    FLAGS.mesh_scale          = 2.1                      # Scale of tet grid box. Adjust to cover the model
    
    FLAGS.env_scale           = 1.0                      # Env map intensity multiplier
    FLAGS.envmap              = None                     # HDR environment probe
    FLAGS.relight             = None                     # HDR environment probe(relight)
    FLAGS.display             = None                     # Conf validation window/display. E.g. [{"relight" : <path to envlight>}]
    FLAGS.camera_space_light  = False                    # Fixed light in camera space. This is needed for setups like ethiopian head where the scanned object rotates on a stand.
    FLAGS.lock_light          = False                    # Disable light optimization in the second pass
    FLAGS.lock_pos            = False                    # Disable vertex position optimization in the second pass
    FLAGS.pre_load            = True                     # Pre-load entire dataset into memory for faster training
    FLAGS.kd_min              = [ 0.0,  0.0,  0.0,  0.0] # Limits for kd
    FLAGS.kd_max              = [ 1.0,  1.0,  1.0,  1.0]
    FLAGS.ks_min              = [ 0.0, 0.08,  0.0]       # Limits for ks
    FLAGS.ks_max              = [ 1.0,  1.0,  1.0]
    FLAGS.nrm_min             = [-1.0, -1.0,  0.0]       # Limits for normal map
    FLAGS.nrm_max             = [ 1.0,  1.0,  1.0]
    FLAGS.cam_near_far        = [1, 50]
    FLAGS.learn_light         = False
    FLAGS.gpu_number          = 1
    FLAGS.sdf_init_shape_scale=[1.0, 1.0, 1.0]
    FLAGS.local_rank = 0
    FLAGS.multi_gpu  = "WORLD_SIZE" in os.environ and int(os.environ["WORLD_SIZE"]) > 1
  
    if FLAGS.config is not None:
        data = json.load(open(FLAGS.config, 'r'))
        for key in data:
            FLAGS.__dict__[key] = data[key]

    if FLAGS.display_res is None:
        FLAGS.display_res = FLAGS.train_res
    if FLAGS.out_dir is None:
        FLAGS.out_dir = 'out/cube_%d' % (FLAGS.train_res)
    else:
        FLAGS.out_dir = 'out/' + FLAGS.out_dir

    if FLAGS.local_rank == 0:
        print("Config / Flags:")
        print("---------")
        for key in FLAGS.__dict__.keys():
            print(key, FLAGS.__dict__[key])
        print("---------")

    seed_everything(FLAGS.seed, FLAGS.local_rank)
    
    os.makedirs(FLAGS.out_dir, exist_ok=True)
    # Rasterization : 벡터 그래픽 형식으로 설명된 이미지를 래스터 이미지로 변환
    # 수학 방정식을 기반으로 점, 직선, 곡선, 다각형과 같은 물체 사용 -> 그 안을 pixel로 채워서 표현
    # glctx = dr.RasterizeGLContext()
    glctx = dr.RasterizeCudaContext()
    # ==============================================================================================
    #  Create data pipeline
    # ==============================================================================================
    dataset_train    = DatasetMesh(glctx, FLAGS, validate=False)
    dataset_validate = DatasetMesh(glctx, FLAGS, validate=True)
    dataset_gif      = DatasetMesh(glctx, FLAGS, gif=True)

    # ==============================================================================================
    #  Create env light with trainable parameters
    # ==============================================================================================
    if FLAGS.learn_light:
        lgt = light.create_trainable_env_rnd(512, scale=0.0, bias=1)
    else:
        # 대부분 여기로 들어갈 텐데, data/irrmaps에 있는 .hdr 파일로 배경 만들기(다운 받아서 보면 암) 
        lgt = light.load_env(FLAGS.envmap, scale=FLAGS.env_scale)

    # SDS Loss로 학습할 때 필요한 Stable Diffusion model load
    stable_diffusion = StableDiffusion(device = 'cuda',
                               text = FLAGS.text,
                               add_directional_text = FLAGS.add_directional_text,
                               batch = FLAGS.batch,
                               stable_diffusion_weight = FLAGS.stable_diffusion_weight,
                               sds_weight_strategy = FLAGS.sds_weight_strategy,
                               early_time_step_range = FLAGS.early_time_step_range,
                               late_time_step_range= FLAGS.late_time_step_range,
                               negative_text = FLAGS.negative_text)
    stable_diffusion.eval()
    for p in stable_diffusion.parameters():
        p.requires_grad_(False)

    # ==============================================================================================
    #  Train with fixed topology (mesh)
    # ==============================================================================================
    # Load initial guess mesh from file
    base_mesh = mesh.load_mesh(FLAGS.base_mesh)
    geometry = DLMesh(base_mesh, FLAGS)

    predicted_material = init_material(geometry, FLAGS)

    geometry, predicted_material = optimize_mesh(glctx, 
                                    geometry, 
                                    predicted_material, 
                                    lgt, 
                                    dataset_train, 
                                    dataset_validate, 
                                    FLAGS, 
                                    optimize_light=FLAGS.learn_light,
                                    optimize_geometry=False, 
                                    stable_diffusion= stable_diffusion,
                                    )

    # ==============================================================================================
    #  Validate
    # ==============================================================================================
    if FLAGS.validate and FLAGS.local_rank == 0:
        if FLAGS.relight != None:       
            relight = light.load_env(FLAGS.relight, scale=FLAGS.env_scale)
        else:
            relight = None
        validate(glctx, geometry, predicted_material, lgt, dataset_gif, os.path.join(FLAGS.out_dir, "validate"), FLAGS, relight)
    if FLAGS.local_rank == 0:
        base_mesh = xatlas_uvmap(glctx, geometry, predicted_material, FLAGS)
    torch.cuda.empty_cache()
    predicted_material['kd_ks_normal'].cleanup()
    del predicted_material['kd_ks_normal']
    lgt = lgt.clone()
    if FLAGS.local_rank == 0:
        os.makedirs(os.path.join(FLAGS.out_dir, "dmtet_mesh"), exist_ok=True)
        obj.write_obj(os.path.join(FLAGS.out_dir, "dmtet_mesh/"), base_mesh)
        light.save_env_map(os.path.join(FLAGS.out_dir, "dmtet_mesh/probe.hdr"), lgt)