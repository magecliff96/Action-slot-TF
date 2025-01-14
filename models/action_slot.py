import torch
import torch.nn as nn
import torchvision.models as models
import torch.nn.functional as F
from classifier import Head, Allocated_Head
from pytorchvideo.models.hub import i3d_r50
from pytorchvideo.models.hub import csn_r101
from pytorchvideo.models.hub import mvit_base_16x4
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
            18, 54, 29, 17, 25,
            64
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
        attn_ori = dots.softmax(dim=1) + self.eps
        attn = attn_ori / attn_ori.sum(dim=-1, keepdim=True)
        slots = torch.einsum('bjd,bij->bid', v, attn)

        slots = slots.reshape(b, -1, d)
        if self.allocated_slot:
            slots = slots[:, :self.num_actor_class, :]
        else:
            slots = slots[:, :self.num_slots, :]
        slots = slots + self.fc2(F.relu(self.fc1(self.norm_pre_ff(slots))))
        return slots, attn_ori

    def forward(self, inputs, num_slots = None):
        b, nf, h, w, d = inputs.shape
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

class DynamicLinear(nn.Module):
    def __init__(self, input_dim):
        super(DynamicLinear, self).__init__()
        self.input_dim = input_dim
        self.fc_layer = None
    def forward(self, x, output_dim):
        # Check if the layer needs to be created or updated
        if self.fc_layer is None or self.fc_layer.out_features != output_dim:
            self.fc_layer = nn.Linear(self.input_dim, output_dim).to(x.device)
        return self.fc_layer(x)

class SelfAttention(nn.Module):
  def __init__(self, input_dim):
    super(SelfAttention, self).__init__()
    self.input_dim = input_dim
    self.query = nn.Linear(input_dim, input_dim) # [batch_size, seq_length, input_dim]
    self.key = nn.Linear(input_dim, input_dim) # [batch_size, seq_length, input_dim]
    self.value = nn.Linear(input_dim, input_dim)
    self.softmax = nn.Softmax(dim=2)
   
  def forward(self, x): # x.shape (batch_size, seq_length, input_dim)
    queries = self.query(x)
    keys = self.key(x)
    values = self.value(x)

    score = torch.bmm(queries, keys.transpose(1, 2))/(self.input_dim**0.5)
    attention = self.softmax(score)
    weighted = torch.bmm(attention, values)
    return weighted



class ACTION_SLOT(nn.Module):
    def __init__(self, args, num_ego_class, num_actor_class, num_slots=21, box=False, videomae=None):
        super(ACTION_SLOT, self).__init__()
        self.hidden_dim = args.channel
        self.hidden_dim2 = args.channel
        self.slot_dim, self.temp_dim = args.channel, args.channel
        self.num_ego_class = num_ego_class
        self.ego_c = 128
        self.num_slots = num_slots
        if args.dataset == 'nuscenes' and args.pretrain == 'oats' and not 'nuscenes'in args.cp:
            num_actor_class = 35
        if args.dataset == 'nuscenes' and args.pretrain == 'oats':
            self.num_slots = 35
        if args.dataset == 'oats' and args.pretrain == 'taco':
            self.num_slots = 64
        # if args.dataset == 'nuscenes' and args.pretrain == 'taco':
        #     self.num_slots = 93
        self.resnet = i3d_r50(True)
        self.args = args


        if args.backbone == 'r50':
            self.resnet = r50.R50()
            self.in_c = 2048
            if args.dataset == 'taco':
                self.resolution = (8, 24)
                self.resolution3d = (args.seq_len, 5, 5)
            elif args.dataset == 'oats':
                self.resolution = (7, 7)
                self.resolution3d = (args.seq_len, 7, 7)

        elif args.backbone == 'i3d':
            self.resnet = self.resnet.blocks[:-1]
            self.in_c = 2048
            if args.dataset == 'taco':
                self.resolution = (8, 24)
                self.resolution3d = (4, 8, 24)
            elif args.dataset == 'oats':
                self.resolution = (7, 7)
                self.resolution3d = (4, 7, 7)

        elif args.backbone == 'x3d':
            self.resnet = torch.hub.load('facebookresearch/pytorchvideo:main', 'x3d_m', pretrained=True)
            self.resnet = self.resnet.blocks[:-1]
            self.in_c = 192
            
            if (args.dataset == 'oats' or args.pretrain == 'oats') and args.pretrain != 'taco':
                self.resolution = (7, 7)
                self.resolution3d = (16, 7, 7)
            else:
                self.resolution = (8, 24)
                self.resolution3d = (16, 8, 24)
            
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
            self.head = Allocated_Head(self.slot_dim, num_ego_class, num_actor_class, self.ego_c)#concat edit
        else:
            self.head = Head(self.slot_dim, num_ego_class, num_actor_class+1, self.ego_c) # concat edit

        if self.num_ego_class != 0:
            self.conv3d_ego = nn.Sequential(
                    nn.ReLU(),
                    nn.BatchNorm3d(self.in_c),
                    nn.Conv3d(self.in_c, self.ego_c, (1, 1, 1), stride=1),
                    )
        if args.backbone == 'r50':
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

        #transformer edits
        #[0,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0]
        self.action_embedding = [
            #C:
            [1,0,0,1,0, 1,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0],#0
            [1,0,0,1,0, 0,1,0,0,0, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0],
            [1,0,0,1,0, 0,0,1,0,0, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0],#2
            [1,0,0,1,0, 0,0,0,1,0, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0],
            [1,0,0,1,0, 0,0,0,0,1, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0],#4
            [1,0,0,1,0, 0,0,0,0,0, 1,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0],
            [1,0,0,1,0, 0,0,0,0,0, 0,1,0,0,0, 0,0,0,0,0, 0,0,0,0,0],#6
            [1,0,0,1,0, 0,0,0,0,0, 0,0,1,0,0, 0,0,0,0,0, 0,0,0,0,0],
            [1,0,0,1,0, 0,0,0,0,0, 0,0,0,1,0, 0,0,0,0,0, 0,0,0,0,0],#8
            [1,0,0,1,0, 0,0,0,0,0, 0,0,0,0,1, 0,0,0,0,0, 0,0,0,0,0],
            [1,0,0,1,0, 0,0,0,0,0, 0,0,0,0,0, 1,0,0,0,0, 0,0,0,0,0],#10
            [1,0,0,1,0, 0,0,0,0,0, 0,0,0,0,0, 0,1,0,0,0, 0,0,0,0,0],##11
            #C+:
            [1,0,0,0,1, 1,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0],#12
            [1,0,0,0,1, 0,1,0,0,0, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0],
            [1,0,0,0,1, 0,0,1,0,0, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0],#14
            [1,0,0,0,1, 0,0,0,1,0, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0],
            [1,0,0,0,1, 0,0,0,0,1, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0],#16
            [1,0,0,0,1, 0,0,0,0,0, 1,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0],
            [1,0,0,0,1, 0,0,0,0,0, 0,1,0,0,0, 0,0,0,0,0, 0,0,0,0,0],#18
            [1,0,0,0,1, 0,0,0,0,0, 0,0,1,0,0, 0,0,0,0,0, 0,0,0,0,0],
            [1,0,0,0,1, 0,0,0,0,0, 0,0,0,1,0, 0,0,0,0,0, 0,0,0,0,0],#20
            [1,0,0,0,1, 0,0,0,0,0, 0,0,0,0,1, 0,0,0,0,0, 0,0,0,0,0],
            [1,0,0,0,1, 0,0,0,0,0, 0,0,0,0,0, 1,0,0,0,0, 0,0,0,0,0],#22
            [1,0,0,0,1, 0,0,0,0,0, 0,0,0,0,0, 0,1,0,0,0, 0,0,0,0,0],##23
            #B
            [0,1,0,1,0, 1,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0],#24
            [0,1,0,1,0, 0,1,0,0,0, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0],
            [0,1,0,1,0, 0,0,1,0,0, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0],#26
            [0,1,0,1,0, 0,0,0,1,0, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0],
            [0,1,0,1,0, 0,0,0,0,1, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0],#28
            [0,1,0,1,0, 0,0,0,0,0, 1,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0],
            [0,1,0,1,0, 0,0,0,0,0, 0,1,0,0,0, 0,0,0,0,0, 0,0,0,0,0],#30
            [0,1,0,1,0, 0,0,0,0,0, 0,0,1,0,0, 0,0,0,0,0, 0,0,0,0,0],
            [0,1,0,1,0, 0,0,0,0,0, 0,0,0,1,0, 0,0,0,0,0, 0,0,0,0,0],#32
            [0,1,0,1,0, 0,0,0,0,0, 0,0,0,0,1, 0,0,0,0,0, 0,0,0,0,0],
            [0,1,0,1,0, 0,0,0,0,0, 0,0,0,0,0, 1,0,0,0,0, 0,0,0,0,0],#34
            [0,1,0,1,0, 0,0,0,0,0, 0,0,0,0,0, 0,1,0,0,0, 0,0,0,0,0],##35
            #B+
            [0,1,0,0,1, 1,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0],#36
            [0,1,0,0,1, 0,1,0,0,0, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0],
            [0,1,0,0,1, 0,0,1,0,0, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0],#38
            [0,1,0,0,1, 0,0,0,1,0, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0],
            [0,1,0,0,1, 0,0,0,0,1, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0],#40
            [0,1,0,0,1, 0,0,0,0,0, 1,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0],
            [0,1,0,0,1, 0,0,0,0,0, 0,1,0,0,0, 0,0,0,0,0, 0,0,0,0,0],#42
            [0,1,0,0,1, 0,0,0,0,0, 0,0,1,0,0, 0,0,0,0,0, 0,0,0,0,0],
            [0,1,0,0,1, 0,0,0,0,0, 0,0,0,1,0, 0,0,0,0,0, 0,0,0,0,0],#44
            [0,1,0,0,1, 0,0,0,0,0, 0,0,0,0,1, 0,0,0,0,0, 0,0,0,0,0],
            [0,1,0,0,1, 0,0,0,0,0, 0,0,0,0,0, 1,0,0,0,0, 0,0,0,0,0],#46
            [0,1,0,0,1, 0,0,0,0,0, 0,0,0,0,0, 0,1,0,0,0, 0,0,0,0,0],##47
            #P
            [0,0,1,1,0, 0,0,0,0,0, 0,0,0,0,0, 0,0,1,0,0, 0,0,0,0,0],#48
            [0,0,1,1,0, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,1,0, 0,0,0,0,0],
            [0,0,1,1,0, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,0,1, 0,0,0,0,0],#50
            [0,0,1,1,0, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0, 1,0,0,0,0],
            [0,0,1,1,0, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0, 0,1,0,0,0],#52
            [0,0,1,1,0, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0, 0,0,1,0,0],
            [0,0,1,1,0, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,1,0],#54
            [0,0,1,1,0, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,0,1],##55
            #P+
            [0,0,1,0,1, 0,0,0,0,0, 0,0,0,0,0, 0,0,1,0,0, 0,0,0,0,0],#56
            [0,0,1,0,1, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,1,0, 0,0,0,0,0],
            [0,0,1,0,1, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,0,1, 0,0,0,0,0],#58
            [0,0,1,0,1, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0, 1,0,0,0,0],
            [0,0,1,0,1, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0, 0,1,0,0,0],#60
            [0,0,1,0,1, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0, 0,0,1,0,0],
            [0,0,1,0,1, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,1,0],#62
            [0,0,1,0,1, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,0,0, 0,0,0,0,1]###64
        ]
        self.action_embedding_tensor = torch.tensor(self.action_embedding, dtype=torch.float32).to(self.args.device)
        self.embedding_fc = DynamicLinear(25).to(self.args.device)
        #self.ego_fc = DynamicLinear(self.ego_c).to(self.args.device)
        #self.combined_fc = DynamicLinear(self.slot_dim*2).to(self.args.device)
        self.SA = SelfAttention(self.slot_dim).to(self.args.device)



    def forward(self, x, box=False):
        seq_len = len(x)
        batch_size = x[0].shape[0]
        height, width = x[0].shape[2], x[0].shape[3]

        if self.args.backbone == 'r50':
            if isinstance(x, list):
                x = torch.stack(x, dim=0) #[T, b, C, h, w]
                x = torch.reshape(x, (seq_len*batch_size, 3, height, width))
                x = self.resnet(x)
                _, c, h, w  = x.shape
                x = torch.reshape(x, (self.args.seq_len, batch_size, c, h, w))
                x = x.permute(1, 2, 0, 3, 4)

        elif self.args.backbone == 'slowfast':
            slow_x = []
            for i in range(0, seq_len, 4):
                slow_x.append(x[i])
            if isinstance(x, list):
                x = torch.stack(x, dim=0) #[v, b, 2048, h, w]
                slow_x = torch.stack(slow_x, dim=0)
                # l, b, c, h, w
                x = x.permute((1,2,0,3,4)) #[b, v, 2048, h, w]
                slow_x = slow_x.permute((1,2,0,3,4))
                x = [slow_x, x]

                for i in range(len(self.resnet)):
                    x = self.resnet[i](x)
                x[1] = self.path_pool(x[1])
                x = torch.cat((x[0], x[1]), dim=1)

        else:
            if isinstance(x, list):
                x = torch.stack(x, dim=0) #[T, b, C, h, w]
                # l, b, c, h, w
                x = x.permute((1,2,0,3,4)) #[b, C, T, h, w]
            for i in range(len(self.resnet)):
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
        
        x = x.permute((0, 2, 3, 4, 1))
        # [bs, n, w, h, c]
        x = torch.reshape(x, (batch_size, new_seq_len, new_h, new_w, -1))
        
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
        attn_masks = attn_masks.permute((0, 2, 1, 3, 4))

        #initializing embedding
        action_embed = self.embedding_fc(self.action_embedding_tensor, x.size(-1))
        action_embed = action_embed.unsqueeze(0).repeat(x.size(0), 1, 1)  # [x(0), 64, 256]

        #x = torch.cat((x, action_embed),dim=-1)
        x = x + action_embed
        #x = self.SA(x)

        #final process
        x = self.drop(x)
        if self.num_ego_class != 0:
            ego_x = self.drop(ego_x)
            ego_x, x = self.head(x, ego_x)
            return ego_x, x, attn_masks
        else:
            x = self.head(x)
            return x, attn_masks