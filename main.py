import numpy as np
import argparse, sys

import torch
from torchvision import transforms
from torchvision.datasets import ImageFolder
from torch.utils.data import DataLoader
import torch.optim as optim
from torch.optim import lr_scheduler
from torch.autograd import Variable

from sklearn.metrics import confusion_matrix, f1_score, accuracy_score, classification_report

import matplotlib
import matplotlib.pyplot as plt

from models import load_model
from samplers import BalancedBatchSampler
from losses import FocalLoss, CoTeachingTripletLoss, CoTeachingLoss, CoTeachingLossPlus
from selectors import HardestNegativeTripletSelector, RandomNegativeTripletSelector, SemihardNegativeTripletSelector
from trainer import fit, train_coteaching, eval_coteaching
from scheduler import adjust_learning_rate
from contanst import *

parser = argparse.ArgumentParser()
parser.add_argument('--seed', type=int, default=1)
parser.add_argument('--train', help='Multualy exclusive with --test. If --model_name1 and --model_name2 are specified, finetuning these model.', action='store_true')
parser.add_argument('--test', help='Multualy exclusive with --train.', action='store_true')
# dataset params
parser.add_argument('--dataset', type=str, help='SAR_8A, SAR_4L, VAIS_RGB, VAIS_IR, VAIS_IR_RGB, ...', default='SAR_8A')
parser.add_argument('--input_size', type=int, help='Resize input image to input_size. If -1, images is in original size (should be use with SPP layer)', default=112)
parser.add_argument('--augment', help='Add data augmentation to training', action='store_false')
# model params
parser.add_argument('--backbone', type=str, help='ResNet50, co_teaching', default='co_teaching')
parser.add_argument('--batch_sampler', type=str, help='balanced, co_teaching', default = 'co_teaching')
parser.add_argument('--loss_fn', type=str, help='co_teaching; co_teaching+; co_teaching_triplet; co_teaching_triplet+', default="co_teaching")
parser.add_argument('--use_classes_weight', action='store_true')
# co-teaching params
parser.add_argument('--keep_rate', type=float, help = 'Keep rate in each mini-batch. Default: 0.7', default = 0.7)
parser.add_argument('--num_gradual', type=int, default = 10, help='how many epochs for linear drop rate, can be 5, 10, 15. This parameter is equal to Tk for R(T) in Co-teaching paper.')
parser.add_argument('--exponent', type = float, default = 1, help='exponent of the forget rate, can be 0.5, 1, 2. This parameter is equal to c in Tc for R(T) in Co-teaching paper.')
# training params
parser.add_argument('--lr', type = float, default = 1e-5)
parser.add_argument('--eval_freq', type=int, default=5)
parser.add_argument('--save_freq', type=int, default=10)
parser.add_argument('--n_epoch', type=int, default=50)
parser.add_argument('--epoch_decay_start', type=int, default=20)
parser.add_argument('--batch_size', type=int, default=8)
# test/finetuning params
parser.add_argument('--model1_name', type=str, help='Name of trained model 1. Default dir: MODEL_DIR', default="")
parser.add_argument('--model1_numclasses', type=int, default=365)
parser.add_argument('--model2_name', type=str, help='Name of trained model 2. Default dir: MODEL_DIR', default="")
parser.add_argument('--model2_numclasses', type=int, default=365)

args = parser.parse_args()

cuda = torch.cuda.is_available()
# Set up data loaders parameters
kwargs = {'num_workers': 4, 'pin_memory': True} if cuda else {} #

# Seed
torch.manual_seed(args.seed)
if cuda:
	torch.cuda.manual_seed(args.seed)

# load datasets
if args.dataset == 'SAR_8A':
	dataset_mean = MEAN_SAR_8A
	dataset_std = STD_SAR_8A
	classes = CLASSES_SAR_8A
	classes_num = NUM_SAR_8A
if args.dataset == 'SAR_4L':
	dataset_mean = MEAN_SAR_4L
	dataset_std = STD_SAR_4L
	classes = CLASSES_SAR_4L
	classes_num = NUM_SAR_4L
if args.dataset == 'VAIS_RGB':
	dataset_mean = MEAN_VAIS_RGB
	dataset_std = STD_VAIS_RGB
	classes = CLASSES_VAIS_RGB
	classes_num = NUM_VAIS_RGB
if args.dataset == "G_FLOOD":
	dataset_mean = MEAN_G_FLOOD
	dataset_std = STD_G_FLOOD
	classes = CLASSES_G_FLOOD
	classes_num = NUM_G_FLOOD
if args.dataset == "MedEval17":
	dataset_mean = MEAN_MEDEVAL17
	dataset_std = STD_MEDEVAL17
	classes = CLASSES_MEDEVAL17
	classes_num = NUM_MEDEVAL17

n_classes = len(classes)

classes_weights = None
if args.use_classes_weight:
	classes_weights = 1.0 - classes_num / sum(classes_num)
	classes_weights = torch.from_numpy(classes_weights).float()
	if cuda:
		classes_weights = classes_weights.cuda()

def run_coeval():
	if args.input_size == -1:
		# do not resize image. should use with SPP layer
		transforms_args = [transforms.ToTensor(), transforms.Normalize(dataset_mean, dataset_std),]
	else:
		transforms_args = [transforms.Resize((args.input_size, args.input_size)), transforms.ToTensor(), transforms.Normalize(dataset_mean, dataset_std),]
	test_dataset = ImageFolder(os.path.join(DATA_DIR, args.dataset, "test"),
							transform=transforms.Compose(transforms_args))

	test_batch_sampler = None
	if args.batch_sampler == "balanced":
		test_batch_sampler = BalancedBatchSampler(torch.from_numpy(np.array(test_dataset.targets)), 
													n_samples=args.batch_size // n_classes, 
													n_batches=50, training=False)
	if test_batch_sampler is not None:
		test_loader = DataLoader(test_dataset, batch_sampler=test_batch_sampler, **kwargs)
	else:
		test_loader = DataLoader(test_dataset, batch_size=args.batch_size, shuffle=False, **kwargs) # default

	return_embedding = False
	if args.loss_fn == "co_teaching_triplet" or args.loss_fn == "co_teaching_triplet+": # metric learning
		return_embedding = True # CNN return embedding instead of logit

	model1 = load_model(args.backbone, n_classes, return_embedding, pt_model_name=args.model1_name, pt_n_classes=args.model1_numclasses)
	model2 = load_model(args.backbone, n_classes, return_embedding, pt_model_name=args.model2_name, pt_n_classes=args.model2_numclasses)
	if cuda:
		model1.cuda()
		model2.cuda()
	
	# test
	with torch.no_grad():
		model1.eval()
		model2.eval()

		logit_1 = np.zeros((len(test_loader.dataset), n_classes))
		logit_2 = np.zeros((len(test_loader.dataset), n_classes))
		labels = np.zeros(len(test_loader.dataset))

		k = 0
		for data, target in test_loader:
			if not type(data) in (tuple, list):
				data = (data,)
			if cuda:
				data = tuple(d.cuda() for d in data)
	        
			logit_1[k: k + len(data[0])] = model1(*data).data.cpu().numpy()
			logit_2[k: k + len(data[0])] = model2(*data).data.cpu().numpy()
			labels[k: k + len(data[0])] = target.numpy()

			k += len(data[0])

		print("Prediction of Model 1")
		preds_1 = np.argmax(logit_1, axis=1)
		print(classification_report(labels, preds_1, target_names=classes))
		print(confusion_matrix(labels, preds_1))

		print("Prediction of Model 2")
		preds_2 = np.argmax(logit_2, axis=1)
		print(classification_report(labels, preds_2, target_names=classes))
		print(confusion_matrix(labels, preds_2))

		print("Joint prediction")
		logit = np.maximum(logit_1, logit_2)
		preds = np.argmax(logit, axis=1)
		print(classification_report(labels, preds, target_names=classes))
		print(confusion_matrix(labels, preds))

def run_coteaching():
	transforms_args = []
	if args.augment:
		transforms_args.append(transforms.RandomCrop(512))
		transforms_args.append(transforms.RandomHorizontalFlip())
		transforms_args.append(transforms.RandomVerticalFlip())
		transforms_args.append(transforms.RandomPerspective())
		transforms_args.append(transforms.RandomRotation(20))
		transforms_args.append(transforms.ColorJitter(hue=.05, saturation=.05))
	if not args.input_size == -1:
		transforms_args.append(transforms.Resize((args.input_size, args.input_size)))
	
	transforms_args.append(transforms.ToTensor())
	transforms_args.append(transforms.Normalize(dataset_mean, dataset_std))

	train_dataset = ImageFolder(os.path.join(DATA_DIR, args.dataset, "train"),
							transform=transforms.Compose(transforms_args))

	test_dataset = ImageFolder(os.path.join(DATA_DIR, args.dataset, "test"),
							transform=transforms.Compose(transforms_args))

	# define drop rate schedule
	rate_schedule = np.ones(args.n_epoch) * args.keep_rate
	rate_schedule[:args.num_gradual] = np.linspace(1.0, args.keep_rate**args.exponent, args.num_gradual)

	# Adjust learning rate and betas for Adam Optimizer
	mom1 = 0.9
	mom2 = 0.1
	alpha_plan = [args.lr] * args.n_epoch
	beta1_plan = [mom1] * args.n_epoch
	for i in range(args.epoch_decay_start, args.n_epoch):
		alpha_plan[i] = float(args.n_epoch - i) / (args.n_epoch - args.epoch_decay_start) * args.lr
		beta1_plan[i] = mom2

	return_embedding = False
	metric_acc = True
	if args.loss_fn == "co_teaching_triplet" or args.loss_fn == "co_teaching_triplet+": # metric learning
		return_embedding = True # CNN return embedding instead of logit
		metric_acc = False # Do not evaluate accuracy during training
		
	train_batch_sampler = None
	test_batch_sampler = None
	if args.batch_sampler == "balanced":
		train_batch_sampler = BalancedBatchSampler(torch.from_numpy(np.array(train_dataset.targets)),
												n_samples=args.batch_size // n_classes,
												n_batches=len(train_dataset.targets) * n_classes // args.batch_size)
		
		test_batch_sampler = BalancedBatchSampler(torch.from_numpy(np.array(test_dataset.targets)), 
													n_samples=args.batch_size // n_classes, 
													n_batches=50, training=False)
		
	if train_batch_sampler is not None:
		train_loader = DataLoader(train_dataset, batch_sampler=train_batch_sampler,**kwargs)
	else:
		train_loader = DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True, **kwargs) # default
	
	if test_batch_sampler is not None:
		test_loader = DataLoader(test_dataset, batch_sampler=test_batch_sampler, **kwargs)
	else:
		test_loader = DataLoader(test_dataset, batch_size=args.batch_size, shuffle=False, **kwargs) # default

	model1 = load_model(args.backbone, n_classes, return_embedding, pt_model_name=args.model1_name, pt_n_classes=args.model1_numclasses)
	model2 = load_model(args.backbone, n_classes, return_embedding, pt_model_name=args.model2_name, pt_n_classes=args.model2_numclasses)
	if cuda:
		model1.cuda()
		model2.cuda()
	
	optimizer1 = optim.Adam(model1.parameters(), lr=args.lr)
	optimizer2 = optim.Adam(model2.parameters(), lr=args.lr)

	if args.loss_fn == "co_teaching":
		print("Training using CoTeachingLoss")
		loss_fn = CoTeachingLoss(weight=classes_weights)
	elif args.loss_fn == "co_teaching+":
		print("Training using CoTeachingLoss+")
		loss_fn = CoTeachingLossPlus(weight=classes_weights)
	elif args.loss_fn == "co_teaching_triplet":
		print("Training using CoTeachingTripletLoss")
		loss_fn = CoTeachingTripletLoss(margin=TRIPLET_MARGIN)
	elif args.loss_fn == "co_teaching_triplet+":
		print("Training using CoTeachingTripletLoss+")
		# TODO: Implement CoTeachingTripletLossPlus
		loss_fn = CoTeachingTripletLoss(margin=TRIPLET_MARGIN)

	epoch = 0
	train_log = []
	for epoch in range(1, args.n_epoch):
		adjust_learning_rate(optimizer1, alpha_plan, beta1_plan, epoch)
		adjust_learning_rate(optimizer2, alpha_plan, beta1_plan, epoch)

		train_loss_1, train_loss_2, total_train_loss_1, total_train_loss_2 = \
			train_coteaching(train_loader, loss_fn, model1, optimizer1, model2, optimizer2, rate_schedule[epoch], cuda)

		if epoch % args.eval_freq == 0:
			test_loss_1, test_loss_2, test_acc_1, test_acc_2 = \
				eval_coteaching(model1, model2, test_loader, loss_fn, cuda, metric_acc=metric_acc)
			
			train_log.append([train_loss_1, train_loss_2, total_train_loss_1, total_train_loss_2, test_loss_1, test_loss_2])
			print('Epoch [%d/%d], Train loss1: %.4f/%.4f, Train loss2: %.4f/%.4f, Test accuracy1: %.4F, Test accuracy2: %.4f, Test loss1: %.4f, Test loss2: %.4f' 
				% (epoch + 1, args.n_epoch, train_loss_1, total_train_loss_1, train_loss_2, total_train_loss_2, test_acc_1, test_acc_2, test_loss_1, test_loss_2))

		if epoch % args.save_freq == 0:
			torch.save(model1.state_dict(), os.path.join(MODEL_DIR, '%s_%s_%.2f_1_%d.pth' % (args.dataset, args.loss_fn, args.keep_rate, epoch)))
			torch.save(model2.state_dict(), os.path.join(MODEL_DIR, '%s_%s_%.2f_2_%d.pth' % (args.dataset, args.loss_fn, args.keep_rate, epoch)))

	# visualize training log
	train_log = np.array(train_log)
	legends = ['train_loss_1', 'train_loss_2', 'total_train_loss_1', 'total_train_loss_2', 'test_loss_1', 'test_loss_2']
	epoch_count = range(1, train_log.shape[0] + 1)
	for i in range(len(legends)):
		plt.loglog(epoch_count, train_log[:, i])
	plt.legend(legends)
	plt.ylabel('loss')
	plt.xlabel('epochs')
	plt.savefig(os.path.join(MODEL_DIR, '%s_%s_%.2f.png' % (args.dataset, args.loss_fn, args.keep_rate)))

if __name__ == '__main__':
	if args.train:
		run_coteaching()
	elif args.test:
		run_coeval()
	else:
		print("Please specify --train, --test (mutualy exclusive).")