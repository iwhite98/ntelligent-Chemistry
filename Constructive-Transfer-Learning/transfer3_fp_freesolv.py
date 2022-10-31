import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader
from rdkit import Chem
import numpy as np
from rdkit.Chem.rdmolops import GetAdjacencyMatrix
from rdkit.Chem.Crippen import MolLogP
from rdkit.Chem import rdMolDescriptors
from sklearn.metrics import roc_auc_score, precision_score, recall_score, accuracy_score
import matplotlib.pyplot as plt
import csv
import warnings
warnings.filterwarnings('ignore')

class DataSet(Dataset):
    
    def __init__(self, smiles_list, freeE_list, max_num_atoms):
        self.smiles_list = smiles_list
        self.freeE_list = torch.from_numpy(np.array(freeE_list))
        self.max_num_atoms = max_num_atoms
        self.feature_list = []
        self.adj_list = []
        self.process_data()

    def one_of_k_encoding(self, x, allowable_set):
        if x not in allowable_set:
            x = allowable_set[-1]
        return list(map(lambda s: x == s, allowable_set))
    
    def get_atom_feature(self, m, atom_i):
        atom = m.GetAtomWithIdx(atom_i)
        atom_feature = np.array(self.one_of_k_encoding(atom.GetSymbol(),['C', 'N', 'O', 'F', 'ELSE'])  + self.one_of_k_encoding(atom.GetFormalCharge(), [-1, 0, 1, 'ELSE']) + self.one_of_k_encoding(atom.GetTotalNumHs(), [0, 1, 2, 3, 4, 'ELSE']) + [atom.GetIsAromatic()])
        return atom_feature 

    def process_data(self):
        max_num_atoms = self.max_num_atoms
        for smiles in self.smiles_list:
            m = Chem.MolFromSmiles(smiles)
            num_atoms = m.GetNumAtoms()
            atom_feature = np.empty((0,16), int)
            adj = GetAdjacencyMatrix(m) + np.eye(num_atoms)
            
            padded_feature = np.zeros((max_num_atoms, 16))
            padded_adj = np.zeros((max_num_atoms, max_num_atoms))
            
            for i in range(num_atoms):
                atom_feature = np.append(atom_feature, [self.get_atom_feature(m, i)], axis = 0)
            
            padded_feature[:num_atoms, :16] = atom_feature
            padded_adj[:num_atoms, :num_atoms] = adj

            self.feature_list.append(torch.from_numpy(padded_feature))
            self.adj_list.append(torch.from_numpy(padded_adj))

                
    def __len__(self):
        return len(self.smiles_list)
    
    def __getitem__(self, idx):
        sample = dict()
        sample["feature"] = self.feature_list[idx]
        sample["adj"] = self.adj_list[idx]
        sample["freeE"] = self.freeE_list[idx]
        return sample

class Transfer(nn.Module):
    
    def __init__(self, n_channel = 128, d_out = 1 , n_conv_layer = 10):
        super().__init__()
        self.activation = nn.ReLU()
        self.embedding = nn.Linear(16, n_channel)
        layer_list = []
        for i in range(n_conv_layer):
            layer_list.append(nn.Linear(n_channel, n_channel))

        self.W = nn.ModuleList(layer_list)
        self.fc = nn.Linear(n_channel, 1024)
        self.linear1 = nn.Linear(1024, 1024)
        self.linear2 = nn.Linear(1024, d_out)


    def load_model(self, pretrian_dict):
        model_dict = self.state_dict()
        load_dict = dict()
        for key in model_dict.keys():
            if key in pretrain_dict.keys():
                load_dict[key] = pretrain_dict[key]
            else:
                load_dict[key] = model_dict[key]
        self.load_state_dict(load_dict, strict = True)


    def freeze(self):
        w_parameters = self.W.parameters()
        for param in w_parameters:
            param.requires_grad = False

    def forward(self, x, A):
        retval = x
        retval = self.embedding(retval)
        for w in self.W:
            retval = w(retval)
            retval = torch.matmul(A, retval)
            retval = self.activation(retval)
        retval = retval.mean(1)
        retval = self.fc(retval)
        retval = torch.sigmoid(retval)
        
        retval = self.linear1(retval)
        retval = torch.relu(retval)
        retval = self.linear2(retval)

        return retval

def load_data(filename = 'freesolv.csv', max_num_atoms = 64):
    
    smiles_list = []
    freeE_list = []
    
    f = open(filename, 'r')
    data = csv.reader(f)
    next(data)
    for line in data:
        smiles = line[1]
        freeE = float(line[-1])
        mol = Chem.MolFromSmiles(smiles)
        if str(type(mol)) == "<class 'NoneType'>":
            continue
        if mol.GetNumAtoms() > max_num_atoms:
            continue
        else:
            smiles_list.append(smiles)
            freeE_list.append(freeE)
                   
    return (smiles_list, freeE_list)

def reduce_lr(loss_list, epoch, lr, optimizer):
    result = False
    
    if epoch != 0:
        if (loss_list[epoch] >= loss_list[epoch-1]):
            result = True

    if epoch >= 5:
        dif = 0
        for i in range(1,5):
            dif += abs(loss_list[epoch - i]-loss_list[epoch-i-1])
        dif = dif/4
        if(dif * 3 < abs(loss_list[epoch]-loss_list[epoch-1])):
            result = True
   
    if result:
        lr = lr * 0.95
        for g in optimizer.param_groups:
            g['lr'] = lr
    
    return lr


n_conv_layers = 3
max_num_atoms = 64

smiles_list, freeE_list = load_data()

num_data = len(smiles_list) ##642
num_test_data = int(num_data * 0.8)
train_smiles = smiles_list[:num_test_data]
test_smiles = smiles_list[num_test_data : num_data]
train_freeE = freeE_list[:num_test_data]
test_freeE = freeE_list[num_test_data : num_data]


train_dataset = DataSet(train_smiles, train_freeE, max_num_atoms)
test_dataset = DataSet(test_smiles, test_freeE, max_num_atoms) 

train_dataloader = DataLoader(train_dataset, batch_size = 32)
test_dataloader = DataLoader(test_dataset, batch_size = 32)

model = Transfer(n_channel = 128, d_out = 1, n_conv_layer = 3)
pretrain_dict = torch.load('GCNtoFP.pt')
model.load_model(pretrain_dict)
model.freeze()
lr = 5e-3
num_epoch = 100
optimizer = torch.optim.Adam(model.parameters(), lr = lr)
loss_fn = nn.MSELoss()

model = model.cuda()
loss_list = []
for epoch in range(num_epoch):
    epoch_loss = np.empty(0,float)
    for i_batch, batch in enumerate(train_dataloader):
        
        x = batch['feature'].cuda().float()
        y = batch['freeE'].cuda().float()
        adj = batch['adj'].cuda().float()
        pred = model(x, adj)
        pred = pred.squeeze(-1)
        loss = loss_fn(pred, y)
        optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        optimizer.step()

        epoch_loss = np.append(epoch_loss, loss.data.cpu())

    print(epoch, ' loss : ',np.mean(epoch_loss))
    loss_list.append(np.mean(epoch_loss))
    lr = reduce_lr(loss_list, epoch, lr, optimizer)

model.eval()
loss_list = np.empty(0, float)
with torch.no_grad():
    for i_batch, batch in enumerate(test_dataloader):
        
        x = batch['feature'].cuda().float()
        y = batch['freeE'].cuda().float()
        adj = batch['adj'].cuda().float()
        pred = model(x, adj)
        pred = pred.squeeze(-1)
        loss = loss_fn(pred, y)
        loss_list = np.append(loss_list, loss.data.cpu())

print('--------freesolv --------')
print('pre-train : GCN to fp')
print('test loss : ', np.mean(loss_list))


