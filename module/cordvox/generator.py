import math

import torch
import torch.nn as nn
import torch.nn.functional as F

from torch.nn.utils.parametrizations import weight_norm


def get_padding(kernel_size, dilation=1):
    return int((kernel_size*dilation - dilation)/2)


def init_weights(m, mean=0.0, std=0.01):
    classname = m.__class__.__name__
    if classname.find("Conv") != -1:
        m.weight.data.normal_(mean, std)


class HarmonicOscillator(nn.Module):
    def __init__(
            self,
            sample_rate=24000,
            frame_size=480,
            num_harmonics=8,
            min_frequency=20.0,
            noise_scale=0.03
        ):
        super().__init__()
        self.sample_rate = sample_rate
        self.frame_size = frame_size
        self.min_frequency = min_frequency
        self.num_harmonics = num_harmonics
        self.noise_scale = noise_scale

        self.weights = nn.Parameter(torch.ones(1, num_harmonics, 1))

    def forward(self, f0):
        with torch.no_grad():
            f0 = F.interpolate(f0, scale_factor=self.frame_size, mode='linear')
            voiced_mask = (f0 >= self.min_frequency).to(torch.float)
            mul = (torch.arange(self.num_harmonics, device=f0.device) + 1).unsqueeze(0).unsqueeze(2)
            fs = f0 * mul
            integrated = torch.cumsum(fs / self.sample_rate, dim=2)
            phi = torch.rand(1, self.num_harmonics, 1, device=f0.device)
            rad = 2 * math.pi * ((integrated + phi) % 1)
            noise = torch.randn_like(rad)
            harmonics = torch.sin(rad) * voiced_mask + noise * self.noise_scale
            voiced_part = harmonics + noise * self.noise_scale
            unvoiced_part = noise * 0.333
            source = voiced_part * voiced_mask + unvoiced_part * (1 - voiced_mask)
            source = (source * F.softmax(self.weights, dim=1)).sum(dim=1, keepdim=True)
        return source


class CyclicNoiseOscillator(nn.Module):
    def __init__(
            self,
            sample_rate=24000,
            frame_size=480,
            min_frequency=20.0,
            base_frequency=110.0,
            beta=0.78
        ):
        super().__init__()
        self.sample_rate = sample_rate
        self.frame_size = frame_size
        self.min_frequency = min_frequency
        self.base_frequency = base_frequency
        self.beta = beta

        self.kernel_size = int(4.6 * self.sample_rate / self.base_frequency)
        self.pad_size = self.kernel_size - 1

    def generate_kernel(self):
        t = torch.arange(0, self.kernel_size)[None, None, :]
        decay = torch.exp(-t * self.base_frequency / self.beta / self.sample_rate)
        decay = decay.flip(dims=[2])
        noise = torch.randn_like(decay)
        kernel = noise * decay
        return kernel

    def forward(self, f0):
        with torch.no_grad():
            f0 = F.interpolate(f0, scale_factor=self.frame_size, mode='linear')
            N = f0.shape[0]
            L = f0.shape[2]
            voiced_mask = (f0 >= self.min_frequency).to(torch.float)
            rad = torch.cumsum(-f0 / self.sample_rate, dim=2)
            sawtooth = rad % 1.0
            impluse = sawtooth - sawtooth.roll(1, dims=(2))
            noise = torch.randn(N, 1, L, device=f0.device)
            kernel = self.generate_kernel().to(f0.device)
            impluse = F.pad(impluse, (0, self.pad_size))
            cyclic_noise = F.conv1d(impluse, kernel)
            source = cyclic_noise * voiced_mask + (1 - voiced_mask) * noise
        return source
    

class ResBlock1(nn.Module):
    def __init__(self, channels, kernel_size=3, dilations=[1, 3, 5]):
        super().__init__()
        self.convs1 = nn.ModuleList()
        self.convs2 = nn.ModuleList()
        for d in dilations:
            self.convs1.append(weight_norm(nn.Conv1d(channels, channels, kernel_size, 1, get_padding(kernel_size, d), d)))
            self.convs2.append(weight_norm(nn.Conv1d(channels, channels, kernel_size, 1, get_padding(kernel_size, 1), 1)))

    def forward(self, x):
        for c1, c2 in zip(self.convs1, self.convs2):
            xt = F.silu(x)
            xt = c1(xt)
            xt = F.silu(xt)
            xt = c2(xt)
            x = x + xt
        return x
    

class ResBlock2(nn.Module):
    def __init__(self, channels, kernel_size=3, dilations=[1, 3]):
        super().__init__()
        self.convs1 = nn.ModuleList()
        for d in dilations:
            self.convs1.append(weight_norm(nn.Conv1d(channels, channels, kernel_size, 1, get_padding(kernel_size, d), d)))

    def forward(self, x):
        for c1 in self.convs1:
            xt = F.silu(x)
            xt = c1(xt)
            x = x + xt
        return x


class FilterNet(nn.Module):
    def __init__(
            self,
            n_mels=80,
            upsample_initial_channels=512,
            resblock_type="1",
            resblock_kernel_sizes=[3, 7, 11],
            resblock_dilations=[[1, 3, 5], [1, 3, 5], [1, 3, 5]],
            upsample_kernel_sizes=[24, 20, 4, 4],
            upsample_rates=[12, 10, 2, 2]
        ):
        super().__init__()
        self.num_kernels = len(resblock_kernel_sizes)
        self.num_upsamples = len(upsample_rates)

        if resblock_type == "1":
            resblock = ResBlock1
        elif resblock_type == "2":
            resblock = ResBlock2
        else:
            raise "invalid resblock type"

        self.conv_pre = weight_norm(nn.Conv1d(n_mels, upsample_initial_channels, 7, 1, 3))
        ch_last = upsample_initial_channels//(2**(self.num_upsamples))
        self.source_pre = weight_norm(nn.Conv1d(1, ch_last, 7, 1, 3))
        self.ups = nn.ModuleList()
        downs = []
        for i, (u, k) in enumerate(zip(upsample_rates, upsample_kernel_sizes)):
            c1 = upsample_initial_channels//(2**i)
            c2 = upsample_initial_channels//(2**(i+1))
            p = (k-u)//2
            downs.append(weight_norm(nn.Conv1d(c2, c1, k, u, p)))
            self.ups.append(weight_norm(nn.ConvTranspose1d(c1, c2, k, u, p)))
        self.downs = nn.ModuleList(reversed(downs))
        
        self.resblocks = nn.ModuleList()
        for i in range(len(self.ups)):
            ch = upsample_initial_channels//(2**(i+1))
            for j, (k, d) in enumerate(zip(resblock_kernel_sizes, resblock_dilations)):
                self.resblocks.append(resblock(ch, k, d))
        self.conv_post = weight_norm(nn.Conv1d(ch, 1, 7, 1, padding=3))

        self.apply(init_weights)


    def forward(self, x, source):
        skips = []
        s = self.source_pre(source)
        for i in range(self.num_upsamples):
            skips.append(s)
            s = F.silu(s)
            s = self.downs[i](s)
        skips = list(reversed(skips))

        x = self.conv_pre(x) + s
        for i in range(self.num_upsamples):
            x = F.silu(x)
            x = self.ups[i](x)
            x = x + skips[i]
            xs = None
            for j in range(self.num_kernels):
                if xs is None:
                    xs = self.resblocks[i*self.num_kernels+j](x)
                else:
                    xs += self.resblocks[i*self.num_kernels+j](x)
            x = xs / self.num_kernels
        x = F.silu(x)
        x = self.conv_post(x)
        x = torch.tanh(x)
        return x
    

class Generator(nn.Module):
    def __init__(self, source, filter, source_type="harmonic"):
        super().__init__()
        if source_type == "harmonic":
            source_net = HarmonicOscillator
        elif source_type == "cyclic_noise":
            source_net = CyclicNoiseOscillator
        else:
            raise "Invalid source_type"
        self.source_net = source_net(**source)
        self.filter_net = FilterNet(**filter)

    def forward(self, x, f0):
        source = self.source_net(f0)
        output = self.filter_net(x, source)
        return output