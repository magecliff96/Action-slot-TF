import torch
import torch.nn as nn
import torchvision.models as models
import torch.nn.functional as F
from classifier import Head, Allocated_Head
from pytorchvideo.models.hub import i3d_r50
from pytorchvideo.models.hub import csn_r101
from pytorchvideo.models.hub import mvit_base_16x4
import inception
import r50
import numpy as np
from math import ceil 
from ptflops import get_model_complexity_info

class SlotAttention(nn.Module):
    def __init__(self, num_slots, dim, num_actor_class=64, eps=1e-8, input_dim=64, resolution=[16, 8, 24], allocated_slot=True):
        super().__init__()
        self.dim = dim
        self.num_slots = num_slots
        self.num_actor_class = num_actor_class
        self.allocated_slot = allocated_slot
        self.eps = eps
        self.scale = dim ** -0.5
        self.resolution = resolution
        self.slots_mu = nn.Parameter(torch.randn(1, 1, dim)).cuda()
        self.slots_sigma = torch.randn(1, 1, dim).cuda()
        self.slots_sigma = nn.Parameter(self.slots_sigma.absolute())


        self.FC1 = nn.Linear(dim, dim)
        self.FC2 = nn.Linear(dim, dim)
        self.LN = nn.LayerNorm(dim)

        self.to_q = nn.Linear(dim, dim)
        self.to_k = nn.Linear(dim, dim)
        self.to_v = nn.Linear(dim, dim)

        self.fc1 = nn.Linear(dim, dim)
        self.fc2 = nn.Linear(dim, dim)
        self.gru = nn.GRUCell(dim, dim)
        
        self.norm_input  = nn.LayerNorm(dim)
        self.norm_slots  = nn.LayerNorm(dim)
        self.norm_pre_ff = nn.LayerNorm(dim)

        mu = self.slots_mu.expand(1, self.num_slots, -1)
        sigma = self.slots_sigma.expand(1, self.num_slots, -1)
        slots = torch.normal(mu, sigma)
        self.pool = nn.AdaptiveAvgPool1d(1)
        self.pe = SoftPositionEmbed3D(dim, [resolution[0], resolution[1], resolution[2]])

        slots = slots.contiguous()
        self.register_buffer("slots", slots)
    def extend_slots(self):
        mu = self.slots_mu.expand(1, 29, -1)
        sigma = self.slots_sigma.expand(1, 29, -1)
        slots = torch.normal(mu, sigma)
        slots = slots.contiguous()

        slots = torch.cat((self.slots[:, :-1, :], slots[:, :, :], torch.reshape(self.slots[:, -1, :], (1, 1, -1))), 1)
        self.register_buffer("slots", slots)

    def extract_slots_for_oats(self):

        oats_slot_idx = [
            13, 12, 50, 6, 3,
            55, 1, 0, 5, 10,
            8, 51, 9, 53, 2,
            4, 48, 59, 52, 61,
            63, 49, 60, 7, 30, 
            11, 57, 22, 62, 58,
            18, 54, 29, 17, 25
            ]
        slots = tuple([torch.reshape(self.slots[:, idx, :], (1, 1, -1)) for idx in oats_slot_idx])
        slots = torch.cat(slots, 1)
        self.register_buffer("slots", slots)

    # def extract_slots_for_nuscenes(self):
    #     slots = torch.cat((self.slots[:, :24, :], slots[:, 33:34, :], self.slots[:, -17:, :]), 1)
        self.register_buffer("slots", slots)
    def get_3d_slot(self, slots, inputs):
        b, l, h, w, d = inputs.shape
        inputs = self.pe(inputs)
        inputs = torch.reshape(inputs, (b, -1, d))

        inputs = self.LN(inputs)
        inputs = self.FC1(inputs)
        inputs = F.relu(inputs)
        inputs = self.FC2(inputs)

        slots_prev = slots
        b, n, d = inputs.shape
        inputs = self.norm_input(inputs)
        k, v = self.to_k(inputs), self.to_v(inputs)
        slots = self.norm_slots(slots)
        q = self.to_q(slots)

        dots = torch.einsum('bid,bjd->bij', q, k) * self.scale
        attn_ori = dots.softmax(dim=-1) + self.eps
        attn = attn_ori / attn_ori.sum(dim=-1, keepdim=True)

        # updates = torch.einsum('bjd,bij->bid', v, attn)
        slots = torch.einsum('bjd,bij->bid', v, attn)
        # slots = self.gru(
        #     updates.reshape(-1, d),
        #     slots_prev.reshape(-1, d)
        # )
        slots = slots.reshape(b, -1, d)
        if self.allocated_slot:
            slots = slots[:, :self.num_actor_class, :]
        else:
            slots = slots[:, :self.num_slots, :]
        slots = slots + self.fc2(F.relu(self.fc1(self.norm_pre_ff(slots))))
        return slots, attn_ori

    def forward(self, inputs, num_slots = None):
        b, nf, h, w, d = inputs.shape
        slots_out = []
        attns = []
        slots = self.slots.expand(b,-1,-1)
        slots_out, attns = self.get_3d_slot(slots, inputs)
        # b, n, c
        return slots_out, attns


def build_3d_grid(resolution):
    ranges = [torch.linspace(0.0, 1.0, steps=res) for res in resolution]
    grid = torch.meshgrid(*ranges)
    grid = torch.stack(grid, dim=-1)
    grid = torch.reshape(grid, [resolution[0], resolution[1], resolution[2], -1])
    grid = grid.unsqueeze(0)
    return torch.cat([grid, 1.0 - grid], dim=-1)


class SoftPositionEmbed3D(nn.Module):
    def __init__(self, hidden_size, resolution):
        """Builds the soft position embedding layer.
        Args:
        hidden_size: Size of input feature dimension.
        resolution: Tuple of integers specifying width and height of grid.
        """
        super().__init__()
        self.embedding = nn.Linear(6, hidden_size, bias=True)
        self.register_buffer("grid", build_3d_grid(resolution))
    def forward(self, inputs):
        grid = self.embedding(self.grid)
        return inputs + grid

class ACTION_SLOT_QUERY(nn.Module):
    def __init__(self, args, num_ego_class, num_actor_class, num_slots=21, box=False, videomae=None):
        super(ACTION_SLOT_QUERY, self).__init__()
        self.hidden_dim = args.channel
        self.hidden_dim2 = args.channel
        self.slot_dim, self.temp_dim = args.channel, args.channel
        self.num_ego_class = num_ego_class
        self.ego_c = 128
        self.num_slots = num_slots
        if args.dataset == 'nuscenes' and args.pretrain == 'oats':
            self.num_slots = 35
        if args.dataset == 'oats' and args.pretrain == 'taco':
            self.num_slots = 64
        # if args.dataset == 'nuscenes' and args.pretrain == 'taco':
        #     self.num_slots = 93
        self.resnet = i3d_r50(True)
        self.args = args
        # self.resnet = self.resnet.blocks[:2]

        # if args.backbone == 'inception':
        #     self.resnet = inception.INCEPTION()
        #     self.in_c = 768
        #     if args.dataset == 'taco':
        #         self.resolution = (8, 24)
        #         self.resolution3d = (args.seq_len, 5, 5)
        #     elif args.dataset == 'oats':
        #         self.resolution = (12, 12)
        #         self.resolution3d = (args.seq_len, 12, 12)

        if args.backbone == 'r50':
            self.resnet = r50.R50()
            self.in_c = 2048
            if args.dataset == 'taco':
                self.resolution = (8, 24)
                self.resolution3d = (args.seq_len, 5, 5)
            elif args.dataset == 'oats':
                self.resolution = (7, 7)
                self.resolution3d = (args.seq_len, 7, 7)

        # elif args.backbone == 'i3d-2':
        #     self.resnet = self.resnet.blocks[:-2]
        #     self.in_c = 1024
        #     if args.dataset == 'taco':
        #         self.resolution = (16, 48)
        #         self.resolution3d = (4, 16, 48)
        #     elif args.dataset == 'oats':
        #         self.resolution = (14, 14)
        #         self.resolution3d = (4, 14, 14)

        elif args.backbone == 'i3d-1':
            self.resnet = self.resnet.blocks[:-1]
            self.in_c = 2048
            if args.dataset == 'taco':
                self.resolution = (8, 24)
                self.resolution3d = (4, 8, 24)
            elif args.dataset == 'oats':
                self.resolution = (7, 7)
                self.resolution3d = (4, 7, 7)

        elif args.backbone == 'x3d-2':
            self.resnet = torch.hub.load('facebookresearch/pytorchvideo:main', 'x3d_m', pretrained=True)
            self.resnet = self.resnet.blocks[:-1]
            self.in_c = 192
            
            if (args.dataset == 'oats' or args.pretrain == 'oats') and args.pretrain != 'taco':
                self.resolution = (7, 7)
                self.resolution3d = (16, 7, 7)
            else:
                self.resolution = (8, 24)
                self.resolution3d = (16, 8, 24)
            
        # elif args.backbone == 'videomae':
        #     self.model = videomae
        #     self.in_c = 768
        #     if args.dataset == 'taco':
        #         self.resolution = (14, 14)
        #         self.resolution3d = (8, 14, 14)
            
        # elif args.backbone == 'mvit':
        #     self.model = mvit_base_16x4(True)
        #     self.in_c = 768
        #     self.resolution = (7, 7)
        #     self.resolution3d = (8, 7, 7)

        # elif args.backbone == 'csn':
        #     self.resnet = csn_r101(True)
        #     self.resnet = self.resnet.blocks[:-1]
        #     self.in_c = 2048
        #     if args.dataset == 'taco':
        #         self.resolution = (8, 24)
        #         self.resolution3d = (4, 8, 24)
        #     elif args.dataset == 'oats':
        #         self.resolution = (7, 7)
        #         self.resolution3d = (4, 7, 7)

        elif args.backbone == 'slowfast':
            self.resnet = torch.hub.load('facebookresearch/pytorchvideo:main', 'slowfast_r50', pretrained=True)
            self.resnet = self.resnet.blocks[:-2]
            self.path_pool = nn.AdaptiveAvgPool3d((4, 8, 24))
            self.in_c = 2304
            if args.dataset == 'oats':
                self.resolution = (7, 7)
                self.resolution3d = (4, 7, 7)
            else:
                self.resolution = (8, 24)
                self.resolution3d = (4, 8, 24)
            
        if args.allocated_slot:
            self.head = Allocated_Head(self.slot_dim, num_ego_class, num_actor_class, self.ego_c)
        else:
            self.head = Head(self.slot_dim, num_ego_class, num_actor_class+1, self.ego_c)

        if self.num_ego_class != 0:
            self.conv3d_ego = nn.Sequential(
                    nn.ReLU(),
                    nn.BatchNorm3d(self.in_c),
                    nn.Conv3d(self.in_c, self.ego_c, (1, 1, 1), stride=1),
                    )
        if args.backbone == 'inception' or args.backbone == 'r50':
            self.conv3d = nn.Sequential(
                    nn.ReLU(),
                    nn.BatchNorm3d(self.in_c),
                    nn.Conv3d(self.in_c, self.in_c//2, (1, 1, 1), stride=1),
                    nn.ReLU(),
                    nn.BatchNorm3d(self.in_c//2),
                    nn.Conv3d(self.in_c//2, self.in_c//2, (3, 3, 3), stride=1, padding='same'),
                    nn.ReLU(),
                    nn.BatchNorm3d(self.in_c//2),
                    nn.Conv3d(self.in_c//2, self.in_c//2, (3, 3, 3), stride=1, padding='same'),
                    nn.ReLU(),
                    nn.BatchNorm3d(self.in_c//2),
                    nn.Conv3d(self.in_c//2, self.hidden_dim2, (1, 1, 1), stride=1),
                    nn.ReLU(),)
        else:
            self.conv3d = nn.Sequential(
                    nn.ReLU(),
                    nn.BatchNorm3d(self.in_c),
                    nn.Conv3d(self.in_c, self.hidden_dim2, (1, 1, 1), stride=1),
                    nn.ReLU(),)

        if args.bg_slot:
            self.slot_attention = SlotAttention(
                num_slots=self.num_slots+1,
                dim=self.slot_dim,
                eps = 1e-8,
                input_dim=self.hidden_dim2,
                resolution=self.resolution3d,
                num_actor_class = num_actor_class
                ) 
        else:
            self.slot_attention = SlotAttention(
                num_slots=self.num_slots,
                dim=self.slot_dim,
                eps = 1e-8,
                input_dim=self.hidden_dim2,
                resolution=self.resolution3d,
                num_actor_class = num_actor_class
                ) 

        self.drop = nn.Dropout(p=0.5)         
        self.pool = nn.AdaptiveAvgPool3d(output_size=1)


    def forward(self, x, box=False):
        seq_len = len(x)
        batch_size = x[0].shape[0]
        height, width = x[0].shape[2], x[0].shape[3]

        
        if self.args.backbone == 'inception' or self.args.backbone == 'r50':
            if isinstance(x, list):
                x = torch.stack(x, dim=0) #[T, b, C, h, w]
                x = torch.reshape(x, (seq_len*batch_size, 3, height, width))

        elif self.args.backbone != 'slowfast':
            if isinstance(x, list):
                x = torch.stack(x, dim=0) #[T, b, C, h, w]
                # l, b, c, h, w
                x = torch.permute(x, (1,2,0,3,4)) #[b, C, T, h, w]
        
        # ---- backbone forward ----
        if self.args.backbone == 'slowfast':
            slow_x = []
            for i in range(0, seq_len, 4):
                slow_x.append(x[i])
            if isinstance(x, list):
                x = torch.stack(x, dim=0) #[v, b, 2048, h, w]
                slow_x = torch.stack(slow_x, dim=0)
                # l, b, c, h, w
                x = torch.permute(x, (1,2,0,3,4)) #[b, v, 2048, h, w]
                slow_x = torch.permute(slow_x, (1,2,0,3,4))
                x = [slow_x, x]

                for i in range(len(self.resnet)):
                    x = self.resnet[i](x)
                x[1] = self.path_pool(x[1])
                x = torch.cat((x[0], x[1]), dim=1)

        elif self.args.backbone == 'mvit':
            x = self.model.patch_embed(x) # torch.Size([8, 25088, 96])
            # x = self.model.cls_positional_encoding(x) # torch.Size([8, 25089, 96])
            # x = self.model.pos_drop(x)
            x = self.model.cls_positional_encoding(x)
            thw = self.model.cls_positional_encoding.patch_embed_shape
            for blk in self.model.blocks:
                x, thw = blk(x, thw) # B, D_index, N, N (N = *thw+1)
            x = self.model.norm_embed(x)[:,1:]
            x = x.reshape(batch_size,self.resolution3d[0],self.resolution3d[1],self.resolution3d[2],-1) # B,T,H,W,C
            x = x.permute(0,4,1,2,3) # B,C,T,H,W
            
        elif self.args.backbone == 'videomae':
            x = self.model.forward_features(x,pretrained=True) # B,TxHxW,C
            x = x.reshape(batch_size,self.resolution3d[0],self.resolution3d[1],self.resolution3d[2],-1) # B,T,H,W,C
            x = x.permute(0,4,1,2,3) # B,C,T,H,W
        elif self.args.backbone == 'inception' or self.args.backbone == 'r50':
            x = self.resnet(x)
            _, c, h, w  = x.shape
            x = torch.reshape(x, (self.args.seq_len, batch_size, c, h, w))
            x = x.permute(1, 2, 0, 3, 4)
        else:
            for i in range(len(self.resnet)):
                # x = self.resnet.blocks[i](x)
                x = self.resnet[i](x)
        # b,c,t,h,w
        x = self.drop(x)
        if self.num_ego_class != 0:
            ego_x = self.conv3d_ego(x)
            ego_x = self.pool(ego_x)
            ego_x = torch.reshape(ego_x, (batch_size, self.ego_c))

        new_seq_len = x.shape[2]
        new_h, new_w = x.shape[3], x.shape[4]

        # # [b, c, n , w, h]
        x = self.conv3d(x)

        
        x = torch.permute(x, (0, 2, 3, 4, 1))
        # [bs, n, w, h, c]
        x = torch.reshape(x, (batch_size, new_seq_len, new_h, new_w, -1))

        # flops latency

        # macs, _ = get_model_complexity_info(self.slot_attention, (16, 8, 24, 128), as_strings=True, print_per_layer_stat=False, verbose=True)
        # print(macs)

        # starter, ender = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
        # repetitions = 300
        # timings=np.zeros((repetitions,1))
        # #GPU-WARM-UP
        # for _ in range(10):
        #     _ = self.slot_attention(x)
        # MEASURE PERFORMANCE
        # with torch.no_grad():
        #     for rep in range(repetitions):
        #         starter.record()
        #         _, _ = self.slot_attention(x)
        #         ender.record()
        #         # WAIT FOR GPU SYNC
        #         torch.cuda.synchronize()
        #         curr_time = starter.elapsed_time(ender)
        #         timings[rep] = curr_time

        # mean_syn = np.sum(timings) / repetitions
        # std_syn = np.std(timings)
        # print(mean_syn)
        
        x, attn_masks = self.slot_attention(x)

        

        # no pool, 3d slot
        b, n, thw = attn_masks.shape
        attn_masks = attn_masks.reshape(b, n, -1)
        attn_masks = attn_masks.view(b, n, new_seq_len, self.resolution[0], self.resolution[1])
        attn_masks = attn_masks.unsqueeze(-1)
        # b*s, n, 4, h, w, 1
        attn_masks = attn_masks.reshape(b, n, -1)
        # b*s, n, 4*h*w
        attn_masks = attn_masks.view(b, n, new_seq_len, self.resolution[0], self.resolution[1])
        # b*s, n, 4, h, w
        attn_masks = attn_masks.unsqueeze(-1)
        # b*s, n, 4, h, w, 1
        attn_masks = attn_masks.view(b*n, 1, new_seq_len, attn_masks.shape[3], attn_masks.shape[4])
        # b, n, t, h, w
        if seq_len > new_seq_len:
            attn_masks = F.interpolate(attn_masks, size=(seq_len, new_h, new_w), mode='trilinear')
        # b, l, n, h, w
        attn_masks = torch.reshape(attn_masks, (b, n, seq_len, new_h, new_w))
        attn_masks = torch.permute(attn_masks, (0, 2, 1, 3, 4))


        # x = torch.sum(x, 1)
        x = self.drop(x)
        if self.num_ego_class != 0:
            ego_x = self.drop(ego_x)
            ego_x, x = self.head(x, ego_x)
            return ego_x, x, attn_masks
        else:
            x = self.head(x)
            return x, attn_masks
