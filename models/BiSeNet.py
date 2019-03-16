#------------------------------------------------------------------------------
#  Libraries
#------------------------------------------------------------------------------
import torch
import torch.nn as nn
import torch.nn.functional as F

from base.base_model import BaseModel
from models.backbonds import ResNet


#------------------------------------------------------------------------------
#  Convolutional block
#------------------------------------------------------------------------------
class ConvBlock(nn.Module):
	def __init__(self, in_channels, out_channels, kernel_size, stride=1, padding=0, bias=True):
		super(ConvBlock, self).__init__()
		self.conv = nn.Conv2d(in_channels, out_channels, kernel_size, stride=stride, padding=padding, bias=bias)
		self.bn = nn.BatchNorm2d(out_channels)

	def forward(self, input):
		x = self.conv(input)
		x = self.bn(x)
		x = F.relu(x, inplace=True)
		return x


#------------------------------------------------------------------------------
#  Spatial Path
#------------------------------------------------------------------------------
class SpatialPath(nn.Module):
	def __init__(self):
		super(SpatialPath, self).__init__()
		self.conv1 = ConvBlock(in_channels=3  , out_channels=64 , kernel_size=3, stride=2, padding=1)
		self.conv2 = ConvBlock(in_channels=64 , out_channels=128, kernel_size=3, stride=2, padding=1)
		self.conv3 = ConvBlock(in_channels=128, out_channels=256, kernel_size=3, stride=2, padding=1)

	def forward(self, input):
		x = self.conv1(input)
		x = self.conv2(x)
		x = self.conv3(x)
		return x


#------------------------------------------------------------------------------
#  Attention Refinement Module
#------------------------------------------------------------------------------
class Attention(nn.Module):
	def __init__(self, in_channels):
		super(Attention, self).__init__()
		self.conv = nn.Conv2d(in_channels, in_channels, kernel_size=1, bias=True)

	def forward(self, input):
		x = F.adaptive_avg_pool2d(input, (1,1))
		x = self.conv(x)
		x = torch.sigmoid(x)
		x = torch.mul(input, x)
		return x


#------------------------------------------------------------------------------
#  Feature Fusion Module
#------------------------------------------------------------------------------
class Fusion(nn.Module):
	def __init__(self, in_channels1, in_channels2, num_classes, kernel_size=3):
		super(Fusion, self).__init__()
		in_channels = in_channels1 + in_channels2
		self.convblock = ConvBlock(in_channels, num_classes, kernel_size, padding=1)
		self.conv1 = nn.Conv2d(num_classes, num_classes, kernel_size=1, bias=True)
		self.conv2 = nn.Conv2d(num_classes, num_classes, kernel_size=1, bias=True)

	def forward(self, input1, input2):
		input = torch.cat([input1, input2], dim=1)
		input = self.convblock(input)
		x = F.adaptive_avg_pool2d(input, (1,1))
		x = self.conv1(x)
		x = F.relu(x, inplace=True)
		x = self.conv2(x)
		x = torch.sigmoid(x)
		x = torch.mul(input, x)
		x = torch.add(input, x)
		return x


#------------------------------------------------------------------------------
#  BiSeNet
#------------------------------------------------------------------------------
class BiSeNet(BaseModel):
	def __init__(self, backbone='resnet18', num_classes=21, pretrained_backbone=None):
		super(BiSeNet, self).__init__()
		if backbone=='resnet18':
			self.spatial_path = SpatialPath()
			self.context_path = ResNet.resnet18(num_classes=None)
			self.low_feat_names = 'layer3'
			self.arm_os16 = Attention(in_channels=256)
			self.arm_os32 = Attention(in_channels=512)
			self.ffm = Fusion(in_channels1=256, in_channels2=768, num_classes=num_classes, kernel_size=3)
			self.conv_final = nn.Conv2d(in_channels=num_classes, out_channels=num_classes, kernel_size=1)
			self.sup_os16 = nn.Conv2d(in_channels=256, out_channels=num_classes, kernel_size=1)
			self.sup_os32 = nn.Conv2d(in_channels=512, out_channels=num_classes, kernel_size=1)
		else:
			raise NotImplementedError

		self._init_weights()
		if pretrained_backbone is not None:
			self.context_path._load_pretrained_model(pretrained_backbone)


	def forward(self, input):
		# Spatial path
		feat_spatial = self.spatial_path(input)

		# Context path
		feat_os32, feat_os16 = self.context_path(input, feature_names=self.low_feat_names)
		feat_gap = F.adaptive_avg_pool2d(feat_os32, (1,1))

		feat_os16 = self.arm_os16(feat_os16)
		feat_os32 = self.arm_os32(feat_os32)
		feat_os32 = torch.mul(feat_os32, feat_gap)
		
		feat_os16 = F.interpolate(feat_os16, scale_factor=2, mode='bilinear', align_corners=False)
		feat_os32 = F.interpolate(feat_os32, scale_factor=4, mode='bilinear', align_corners=False)
		feat_context = torch.cat([feat_os16, feat_os32], dim=1)

		# Supervision
		if self.training:
			feat_os16_sup = self.sup_os16(feat_os16)
			feat_os32_sup = self.sup_os32(feat_os32)
			feat_os16_sup = F.interpolate(feat_os16_sup, scale_factor=8, mode='bilinear', align_corners=False)
			feat_os32_sup = F.interpolate(feat_os32_sup, scale_factor=8, mode='bilinear', align_corners=False)

		# Fusion
		x = self.ffm(feat_spatial, feat_context)
		x = F.interpolate(x, scale_factor=8, mode='bilinear', align_corners=False)
		x = self.conv_final(x)

		# Output
		if self.training:
			return x, feat_os16_sup, feat_os32_sup
		else:
			return x


	def _init_weights(self):
		for m in self.modules():
			if isinstance(m, nn.Conv2d):
				nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
			elif isinstance(m, nn.BatchNorm2d):
				nn.init.constant_(m.weight, 1)
				nn.init.constant_(m.bias, 0)