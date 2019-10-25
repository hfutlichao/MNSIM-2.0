#-*-coding:utf-8-*-
import torch
import torch.nn as nn
from torch.autograd import Function
import torch.nn.functional as F
import numpy as np
import math
import copy
import collections

# last_activation_scale, last_weight_scale 代表activation和weight在上次计算的放缩比例
# last_activation_bit, last_weight_bit 代表activation和weight在上次计算的比特数
global last_activation_scale
global last_weight_scale
global last_activation_bit
global last_weight_bit
# quantize Function
class QuantizeFunction(Function):
    @staticmethod
    def forward(ctx, input, qbit, mode, last_value = None, training = None):
        global last_weight_scale
        global last_activation_scale
        global last_weight_bit
        global last_activation_bit
        # last_value是上一次的最大值矩阵，training_flag是training的标志，
        # 如果是训练，那么last_value中的值要发生变化，否则以last_value中的值为准
        # weight可以采用浮点数定点，但是activation不行，因为是在计算过程中
        if mode == 'weight':
            last_weight_bit = qbit
            scale = torch.max(torch.abs(input)).item()
        elif mode == 'activation':
            last_activation_bit = qbit
            if training:
                ratio = 0.707
                tmp = last_value.item()
                tmp = ratio * tmp + (1 - ratio) * torch.max(torch.abs(input)).item()
                last_value.data[0] = tmp
            scale = last_value.data[0]
        else:
            assert 0, 'not support %s' % mode
        # transfer
        thres = 2 ** (qbit - 1) - 1
        output = input / scale
        output = torch.clamp(torch.round(output * thres), 0 - thres, thres - 0)
        output = output * scale / thres
        if mode == 'weight':
            last_weight_scale = scale / thres
        elif mode == 'activation':
            last_activation_scale = scale / thres
        else:
            assert 0, 'not support %s' % mode
        return output
    @staticmethod
    def backward(ctx, grad_output):
        return grad_output, None, None, None, None
Quantize = QuantizeFunction.apply

# # 和ReRAM相关的部分参数
# AB = 1
# N = 512
# Q = 10
# # METHOD = 'TRADITION'
# # METHOD = 'FIX_TRAIN'
# # METHOD = 'SINGLE_FIX_TEST'
# METHOD = ''
# 量化层，目前包含卷积层和全连接层，且这两层均不支持带有bias
class QuantizeLayer(nn.Module):
    def __init__(self, hardware_config, layer_config, quantize_config):
        super(QuantizeLayer, self).__init__()
        # load hardware layer and quantize config in setting
        self.hardware_config = copy.deepcopy(hardware_config)
        self.layer_config = copy.deepcopy(layer_config)
        self.quantize_config = copy.deepcopy(quantize_config)
        # 将卷积层进行分块，主要是对输入channel进行分块，对conv和fc有不同的实现
        if self.layer_config['type'] == 'conv':
            channel_N = (self.hardware_config['xbar_size'] // (self.layer_config['kernel_size'] ** 2))
            complete_bar_num = self.layer_config['in_channels'] // channel_N
            residual_col_num = self.layer_config['in_channels'] % channel_N
            in_channels_list = []
            if residual_col_num > 0:
                in_channels_list = [channel_N] * complete_bar_num + [residual_col_num]
            else:
                in_channels_list = [channel_N] * complete_bar_num
            # 按照参数生成Module List
            self.layer_list = nn.ModuleList([nn.Conv2d(i, self.layer_config['out_channels'], self.layer_config['kernel_size'],
                                                        stride = 1, padding = 0, dilation = 1, groups = 1, bias = False)
                                                        for i in in_channels_list])
            self.split_input = channel_N
        elif self.layer_config['type'] == 'fc':
            complete_bar_num = self.layer_config['in_features'] // self.hardware_config['xbar_size']
            residual_col_num = self.layer_config['in_features'] % self.hardware_config['xbar_size']
            if residual_col_num > 0:
                in_features_list = [self.hardware_config['xbar_size']] * complete_bar_num + [residual_col_num]
            else:
                in_features_list = [self.hardware_config['xbar_size']] * complete_bar_num
            # 按照参数生成Module List
            self.layer_list = nn.ModuleList([nn.Linear(i, self.layer_config['out_features'], False) for i in in_features_list])
            self.split_input = self.hardware_config['xbar_size']
        else:
            raise NotImplementedError
        # 用来存储权重比特数和scale等相关信息
        self.last_value = nn.Parameter(torch.zeros(1))
        self.bit_scale_list = nn.Parameter(torch.zeros(3, 2))
        # 输入输出尺寸和层信息
        self.input_shape = torch.Size()
        self.output_shape = torch.Size()
        self.layer_info = None

    def structure_forward(self, input):
        # test in TRADITION
        self.input_shape = input.shape
        input_list = torch.split(input, self.split_input, dim = 1)
        output = None
        for i in range(len(self.layer_list)):
            if i == 0:
                output = self.layer_list[i](input_list[i])
            else:
                output.add_(self.layer_list[i](input_list[i]))
        self.output_shape = output.shape
        # layer_info
        self.layer_info = collections.OrderedDict()
        if self.layer_config['type'] == 'conv':
            self.layer_info['type'] = 'conv'
            self.layer_info['Inputsize'] = list(self.input_shape)[2:]
            self.layer_info['Outputsize'] = list(self.output_shape)[2:]
            self.layer_info['Kernelsize'] = self.layer_config['kernel_size']
            self.layer_info['Stride'] = 1
            self.layer_info['Inputchannel'] = int(self.input_shape[1])
            self.layer_info['Outputchannel'] = int(self.output_shape[1])
        elif self.layer_config['type'] == 'fc':
            self.layer_info['type'] = 'fc'
            self.layer_info['Infeature'] = int(self.input_shape[1])
            self.layer_info['Outfeature'] = int(self.input_shape[1])
        self.layer_info['Inputbit'] = int(self.bit_scale_list[0,0].item())
        self.layer_info['Weightbit'] = int(self.bit_scale_list[1,0].item())
        self.layer_info['outputbit'] = int(self.bit_scale_list[2,0].item())
        self.layer_info['row_split_num'] = len(self.layer_list)
        self.layer_info['weight_cycle'] = (int(self.bit_scale_list[1, 0].item()) - 1) // (self.hardware_config['weight_bit'])
        return output

    def forward(self, input):
        METHOD = self.hardware_config['fix_method']
        # 传统方法，直接将结果拼接即可
        if METHOD == 'TRADITION':
            input_list = torch.split(input, self.split_input, dim = 1)
            output = None
            for i in range(len(self.layer_list)):
                if i == 0:
                    output = self.layer_list[i](input_list[i])
                else:
                    output.add_(self.layer_list[i](input_list[i]))
            return output
        # 常规的定点方法，为了保证定点位数的正确性和简化操作，将所有的weight拼接在一起
        if METHOD == 'FIX_TRAIN':
            weight = torch.cat([l.weight for l in self.layer_list], dim = 1)
            # 在训练过程中采用简单的定点策略进行定点，先对weight进行定点
            global last_weight_scale
            global last_activation_scale
            global last_weight_bit
            global last_activation_bit
            # last activation bit and scale
            self.bit_scale_list.data[0, 0] = last_activation_bit
            self.bit_scale_list.data[0, 1] = last_activation_scale
            weight = Quantize(weight, self.quantize_config['weight_bit'], 'weight', None, None)
            # weight bit and scale
            self.bit_scale_list.data[1, 0] = last_weight_bit
            self.bit_scale_list.data[1, 1] = last_weight_scale
            if self.layer_config['type'] == 'conv':
                output = F.conv2d(input, weight, None, 1, 0, 1, 1)
            elif self.layer_config['type'] == 'fc':
                output = F.linear(input, weight, None)
            else:
                raise NotImplementedError
            output = Quantize(output, self.quantize_config['activation_bit'], 'activation', self.last_value, self.training)
            # output activation bit and scale
            self.bit_scale_list.data[2, 0] = last_activation_bit
            self.bit_scale_list.data[2, 1] = last_activation_scale
            return output
        if METHOD == 'SINGLE_FIX_TEST':
            assert self.training == False
            output = None
            # 在测试过程中采用单比特定点策略进行训练
            input_list = torch.split(input, self.split_input, dim = 1)
            scale = self.last_value.item()
            # 先得到权重最大值，相当于在外部做了一遍权重的定点操作
            weight_bit = int(self.bit_scale_list[1, 0].item())
            weight_scale = self.bit_scale_list[1, 1].item()
            for layer_num, l in enumerate(self.layer_list):
                # transfer part weight
                thres = 2 ** (weight_bit - 1) - 1
                weight_digit = torch.clamp(torch.round(l.weight / weight_scale), 0 - thres, thres - 0)
                # 对weight进行分解
                assert (weight_bit - 1) % self.hardware_config['weight_bit'] == 0
                weight_cycle = (weight_bit - 1) // self.hardware_config['weight_bit']
                sign_weight = torch.sign(weight_digit)
                weight_digit = torch.abs(weight_digit)
                base = 1
                step = 2 ** self.hardware_config['weight_bit']
                weight_container = []
                for j in range(weight_cycle):
                    tmp = torch.fmod(weight_digit, base * step) - torch.fmod(weight_digit, base)
                    weight_container.append(torch.mul(sign_weight, tmp) * weight_scale)
                    base = base * step
                # 根据存储的结果来计算
                activation_in_bit = int(self.bit_scale_list[0, 0].item())
                activation_in_scale = self.bit_scale_list[0, 1].item()
                thres = 2 ** (activation_in_bit - 1) - 1
                activation_in_digit = torch.clamp(torch.round(input_list[layer_num] / activation_in_scale), 0 - thres, thres - 0)
                assert (activation_in_bit - 1) % self.hardware_config['input_bit'] == 0
                activation_in_cycle = (activation_in_bit - 1) // self.hardware_config['input_bit']
                # 对activation_in进行分解
                sign_activation_in = torch.sign(activation_in_digit)
                activation_digit_in = torch.abs(activation_in_digit)
                base = 1
                step = 2 ** self.hardware_config['input_bit']
                activation_in_container = []
                for i in range(activation_in_cycle):
                    tmp = torch.fmod(activation_in_digit, base * step) -  torch.fmod(activation_in_digit, base)
                    activation_in_container.append(torch.mul(sign_activation_in, tmp) * activation_in_scale)
                    base = base * step
                # 对划分完成的weight和input进行逐顺序计算，对最终的结果进行拼接, scale变化，Q比特截取
                point_shift = self.quantize_config['point_shift']
                Q = self.hardware_config['quantize_bit']
                for i in range(activation_in_cycle):
                    for j in range(weight_cycle):
                        tmp = None
                        if self.layer_config['type'] == 'conv':
                            tmp = F.conv2d(activation_in_container[i], weight_container[j], None, 1, 0, 1, 1)
                        elif self.layer_config['type'] == 'fc':
                            tmp = F.linear(activation_in_container[i], weight_container[j], None)
                        else:
                            raise NotImplementedError
                        # 除以scale，得到的结果应该在某个范围以内
                        tmp = tmp / scale
                        # 计算此种情况下的小数点偏移，由于存在符号位，最后量化为Q - 1
                        transfer_point = point_shift + (activation_in_cycle - 1 - i) * self.hardware_config['input_bit'] + (weight_cycle - 1 - j) * self.hardware_config['weight_bit'] + (Q - 1)
                        tmp = tmp * (2 ** transfer_point)
                        tmp = torch.clamp(torch.round(tmp), 1 - 2 ** (Q - 1), 2 ** (Q - 1) - 1)
                        tmp = tmp / (2 ** transfer_point)
                        # 之后将结果累计即可
                        if torch.is_tensor(output):
                            output = output + tmp
                        else:
                            output = tmp
            # 此时output为小数，需要进行定点量化
            activation_out_bit = int(self.bit_scale_list[0, 0].item())
            activation_out_scale = self.bit_scale_list[0, 1].item()
            thres = 2 ** (activation_out_bit - 1) - 1
            output = torch.clamp(torch.round(output * thres), 0 - thres, thres - 0)
            output = output * scale / thres
            return output
        assert 0
    def get_bit_weights(self):
        assert self.hardware_config['fix_method'] == 'SINGLE_FIX_TEST' or self.hardware_config['fix_method'] == 'FIX_TRAIN'
        bit_weights = collections.OrderedDict()
        # 先得到权重最大值，相当于在外部做了一遍权重的定点操作
        weight_bit = int(self.bit_scale_list[1, 0].item())
        weight_scale = self.bit_scale_list[1, 1].item()
        for layer_num, l in enumerate(self.layer_list):
            assert (weight_bit - 1) % self.hardware_config['weight_bit'] == 0
            weight_cycle = (weight_bit - 1) // self.hardware_config['weight_bit']
            # transfer part weight
            thres = 2 ** (weight_bit - 1) - 1
            weight_digit = torch.clamp(torch.round(l.weight / weight_scale), 0 - thres, thres - 0)
            # 对weight进行分解
            sign_weight = torch.sign(weight_digit)
            weight_digit = torch.abs(weight_digit)
            base = 1
            step = 2 ** self.hardware_config['weight_bit']
            for j in range(weight_cycle):
                tmp = torch.fmod(weight_digit, base * step) - torch.fmod(weight_digit, base)
                tmp = torch.mul(sign_weight, tmp)
                tmp = copy.deepcopy((tmp / base).detach().cpu().numpy())
                bit_weights[f'split{layer_num}_weight{j}_positive'] = np.where(tmp > 0, tmp, 0)
                bit_weights[f'split{layer_num}_weight{j}_negative'] = np.where(tmp < 0, -tmp, 0)
                base = base * step
        return bit_weights
    def set_weights_forward(self, input, bit_weights):
        assert self.hardware_config['fix_method'] == 'SINGLE_FIX_TEST'
        assert self.training == False
        output = None
        # 在测试过程中采用单比特定点策略进行训练
        input_list = torch.split(input, self.split_input, dim = 1)
        scale = self.last_value.item()
        weight_bit = int(self.bit_scale_list[1, 0].item())
        weight_scale = self.bit_scale_list[1, 1].item()
        for layer_num, l in enumerate(self.layer_list):
            assert (weight_bit - 1) % self.hardware_config['weight_bit'] == 0
            weight_cycle = (weight_bit - 1) // self.hardware_config['weight_bit']
            weight_container = []
            base = 1
            step = 2 ** self.hardware_config['weight_bit']
            for j in range(weight_cycle):
                tmp = bit_weights[f'split{layer_num}_weight{j}_positive'] - bit_weights[f'split{layer_num}_weight{j}_negative']
                tmp = torch.from_numpy(tmp) * base * weight_scale
                weight_container.append(tmp.to(input.device))
                base = base * step
            # 根据存储的结果来计算
            activation_in_bit = int(self.bit_scale_list[0, 0].item())
            activation_in_scale = self.bit_scale_list[0, 1].item()
            thres = 2 ** (activation_in_bit - 1) - 1
            activation_in_digit = torch.clamp(torch.round(input_list[layer_num] / activation_in_scale), 0 - thres, thres - 0)
            assert (activation_in_bit - 1) % self.hardware_config['input_bit'] == 0
            activation_in_cycle = (activation_in_bit - 1) // self.hardware_config['input_bit']
            # 对activation_in进行分解
            sign_activation_in = torch.sign(activation_in_digit)
            activation_digit_in = torch.abs(activation_in_digit)
            base = 1
            step = 2 ** self.hardware_config['input_bit']
            activation_in_container = []
            for i in range(activation_in_cycle):
                tmp = torch.fmod(activation_in_digit, base * step) -  torch.fmod(activation_in_digit, base)
                activation_in_container.append(torch.mul(sign_activation_in, tmp) * activation_in_scale)
                base = base * step
            # 对划分完成的weight和input进行逐顺序计算，对最终的结果进行拼接, scale变化，Q比特截取
            point_shift = self.quantize_config['point_shift']
            Q = self.hardware_config['quantize_bit']
            for i in range(activation_in_cycle):
                for j in range(weight_cycle):
                    tmp = None
                    if self.layer_config['type'] == 'conv':
                        tmp = F.conv2d(activation_in_container[i], weight_container[j], None, 1, 0, 1, 1)
                    elif self.layer_config['type'] == 'fc':
                        tmp = F.linear(activation_in_container[i], weight_container[j], None)
                    else:
                        raise NotImplementedError
                    # 除以scale，得到的结果应该在某个范围以内
                    tmp = tmp / scale
                    # 计算此种情况下的小数点偏移，由于存在符号位，最后量化为Q - 1
                    transfer_point = point_shift + (activation_in_cycle - 1 - i) * self.hardware_config['input_bit'] + (weight_cycle - 1 - j) * self.hardware_config['weight_bit'] + (Q - 1)
                    tmp = tmp * (2 ** transfer_point)
                    tmp = torch.clamp(torch.round(tmp), 1 - 2 ** (Q - 1), 2 ** (Q - 1) - 1)
                    tmp = tmp / (2 ** transfer_point)
                    # 之后将结果累计即可
                    if torch.is_tensor(output):
                        output = output + tmp
                    else:
                        output = tmp
        # 此时output为小数，需要进行定点量化
        activation_out_bit = int(self.bit_scale_list[0, 0].item())
        activation_out_scale = self.bit_scale_list[0, 1].item()
        thres = 2 ** (activation_out_bit - 1) - 1
        output = torch.clamp(torch.round(output * thres), 0 - thres, thres - 0)
        output = output * scale / thres
        return output
    def extra_repr(self):
        return str(self.hardware_config) + str(self.layer_config) + str(self.quantize_config)