import torch
from torch import nn 
from torch.nn import functional as F

from utils import *

def make_input_n_mask_pairs(x, device): 
    """A fucntion to make pairs of input-mask

    # Parameters
    ____________
    x : dict 
        x['input']: input time-series
        x['mask']: indicator 
    
    # Returns
    _________
    pair : torch-Tensor 
        the shape of the tensor 'pair' is b, 2*c, n, l 
        where:          
            b= batch size of the x['input']          
            c= # chennels of the x['input']        
            n= # time-series of the x['input']        
            l= # time-stamps of the x['input']                   
    """
    b, c, n, p =  x['input'].shape # batch_size, #channel, #time-series, #time-stamps
    pair = torch.zeros((b,2*c,n,p)).to(device)
    pair[:, ::2, ...] = x['input'] 
    pair[:, 1::2, ...] = x['mask']
    return pair

class ResidualAdd(nn.Module):
    """Residual connection

    # Arguments
    ___________
    fn : sub-class of nn.Module          
    
    # Returns
    _________
    returns residual connection           
    """
    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def forward(self, x, **kwargs):
        res = x
        x = self.fn(x, **kwargs)
        x += res
        return x

# projection layer
class ProjectionConv1x1Layer(nn.Module): 
    """Projection layer using conv1x1
    """
    def __init__(self, in_channels, out_channels, groups, **kwargs): 
        super().__init__()     
        self.projection = nn.Conv2d(in_channels, out_channels, 1, 1, 0, 1, groups= groups, **kwargs)
        self.in_channels, self.out_channels = in_channels, out_channels 

    def forward(self, pair): 
        """Feed forward

        pair : torch-Tensor
            generated by the function: make_input_n_mask_pairs(x)
            the shape of the tensor 'pair' is b, 2*c, n, l 
        """
        return self.projection(pair)

# temporal convolution layer
class DilatedInceptionLayer(nn.Module):
    """Dilated inception layer
    """
    def __init__(self, in_channels, out_channels, **kwargs):
        super().__init__()
        self.branch1x1 = nn.Conv2d(in_channels, out_channels, kernel_size= (1,1), padding= (0, 0), groups= in_channels, dilation= 1, **kwargs)
        self.branch1x3 = nn.Conv2d(in_channels, out_channels, kernel_size= (1,3), padding= (0, 1), groups= in_channels, dilation= 1, **kwargs)
        self.branch1x5 = nn.Conv2d(in_channels, out_channels, kernel_size= (1,3), padding= (0, 2), groups= in_channels, dilation= 2, **kwargs)
        self.branch1x7 = nn.Conv2d(in_channels, out_channels, kernel_size= (1,3), padding= (0, 3), groups= in_channels, dilation= 3, **kwargs)

        self.in_channels, self.out_channels = in_channels, out_channels

    def forward(self, x): 
        b, c, n, p = x.shape
        outs = torch.zeros(b, 4*c, n, p).to(x.device)
        for i in range(4): 
            branch = getattr(self, f'branch1x{2*i+1}')
            outs[:, i::4, ...] = branch(x) 
            # we have c groups of receptive channels...
            # = 4 channels form one group.
        return outs

class TemporalConvolutionModule(nn.Module): 
    """TemporalConvolutionModule
    """
    def __init__(self, in_channels, out_channels, num_heteros, **kwargs): 
        super().__init__()
        self.dil_filter = DilatedInceptionLayer(in_channels, out_channels, **kwargs)
        self.dil_gate = DilatedInceptionLayer(in_channels, out_channels, **kwargs) 
        self.conv_inter = nn.Conv2d(4*in_channels, in_channels, 1, groups= num_heteros, **kwargs)
        self.in_channels, self.out_channels = in_channels, out_channels  
        self.num_heteros = num_heteros

    def forward(self, x): 
        out_filter = torch.tanh(self.dil_filter(x))
        out_gate = torch.sigmoid(self.dil_gate(x)) 
        out = out_filter*out_gate
        return self.conv_inter(out)        

# graph learning layer 
class AdjConstructor(nn.Module): 
    """Constructs an adjacency matrix

    n_nodes: the number of nodes (node= cell)
    embedding_dim: dimension of the embedding vector
    """
    def __init__(self, n_nodes, embedding_dim, alpha= 3., top_k= 4): 
        super().__init__()
        self.emb1 = nn.Embedding(n_nodes, embedding_dim=embedding_dim)
        self.emb2 = nn.Embedding(n_nodes, embedding_dim=embedding_dim)
        self.theta1 = nn.Linear(embedding_dim, embedding_dim)
        self.theta2 = nn.Linear(embedding_dim, embedding_dim)
        self.alpha = alpha # controls saturation rate of tanh: activation function.
        self.top_k = top_k
    def forward(self, idx):
        emb1 = self.emb1(idx) 
        emb2 = self.emb2(idx) 

        emb1 = torch.tanh(self.alpha * self.theta1(emb1))
        emb2 = torch.tanh(self.alpha * self.theta2(emb2))

        adj_mat = torch.relu(torch.tanh(self.alpha*(emb1@emb2.T - emb2@emb1.T))) # adjacency matrix
        mask = torch.zeros(idx.size(0), idx.size(0)).to(idx.device) 
        mask.fill_(float('0'))
        if self.training:
            s1, t1 = (adj_mat + torch.rand_like(adj_mat)*0.01).topk(self.top_k, 1) # values, indices
        else: 
            s1, t1 = adj_mat.topk(self.top_k, 1)
        mask.scatter_(1, t1, s1.fill_(1))
        adj_mat = adj_mat * mask 
        return adj_mat

# graph convolution layer 
class InformationPropagtionLayer(nn.Module): 
    """Information Propagtion Layer
    """
    def __init__(self): 
        super().__init__() 

    def forward(self, x, h_in, A_inter, beta= 0.5): 
        """Feed forward
        x : FloatTensor
            input time series 
        A_inter : C x N x N 
            adjacency matrix (inter time series)
        A_outer : C x C 
            adjacency matrix (outer time series)
            yet to be used...
        """
        # obtain normalized adjacency matrices
        A_i = self.norm_adj(A_inter)
        h = torch.matmul(A_i, x) # bs, c, n, l
        # h = self.beta * x + (1-self.beta) * h
        return beta * h_in + (1-beta) * h
        
    def norm_adj(self, A): 
        """Obtains normalized version of an adjacency matrix
        """
        assert len(A.shape) == 3 or len(A.shape) == 2, "Improper shape of an adjacency matrix, must be either C x C or C x N x N"
        with torch.no_grad():
            if len(A.shape) == 3: 
                eyes = torch.stack([torch.eye(A.shape[1]) for _ in range(A.shape[0])]).to(device= A.device)
                D_tilde_inv = torch.diag_embed(1/(1. + torch.sum(A, dim=2))) # C x N x N 
                A_tilde = D_tilde_inv @ (A + eyes)
            else: 
                eye = torch.eye(A.shape[1]).to(A.device)
                D_tilde_inv = torch.diag(1/(1.+torch.sum(A, dim=1)))
                A_tilde = D_tilde_inv @ (A + eye)
        return A_tilde 

# graph convolution module 
class GraphConvolutionModule(nn.Module): 
    """Graph convolution module
    Args:
    in_features : int
        dimension of the input tensor
    out_features : int
        dimension of the output tensor 
    k : int
        the number of layers
    """
    def __init__(self, in_features, out_features, k, **kwargs): 
        super().__init__() 
        # self.conv_inter = nn.Conv2d(in_features, out_features, 1, groups= out_features, **kwargs)

        for i in range(1, k+1):
            setattr(self, f'gcl{i}', InformationPropagtionLayer())

        for i in range(k+1): 
            setattr(self, f'info_select{i}', nn.Conv2d(out_features, out_features, 1,1,0,1,1, **kwargs))
        
        self.in_features, self.out_features, self.k\
            = in_features, out_features, k
    
    def forward(self, x, A, beta= 0.5):
        A_tilde = self.norm_adj(A)
        # x = self.conv_inter(x)
        hiddens = [x]
        gcl = getattr(self, 'gcl1')
        hiddens.append(gcl(hiddens[-1], x, A_tilde))
        for i in range(2, self.k+1): 
            gcl = getattr(self, f'gcl{i}')
            A_tilde = torch.bmm(A_tilde.permute(0,2,1), A)
            hiddens.append(gcl(hiddens[-1], x, A_tilde, beta= beta))
        out = 0
        for i in range(self.k+1):
            info_select = getattr(self, f'info_select{i}')
            out += info_select(hiddens[i])
        return out 

    def norm_adj(self, A): 
        """Obtains normalized version of an adjacency matrix
        """
        assert len(A.shape) == 3 or len(A.shape) == 2, "Improper shape of an adjacency matrix, must be either C x C or C x N x N"
        with torch.no_grad():
            if len(A.shape) == 3: 
                eyes = torch.stack([torch.eye(A.shape[1]) for _ in range(A.shape[0])]).to(device= A.device)
                D_tilde_inv = torch.diag_embed(1/(1. + torch.sum(A, dim=2))) # C x N x N 
                A_tilde = D_tilde_inv @ (A + eyes)
            else: 
                eye = torch.eye(A.shape[1]).to(A.device)
                D_tilde_inv = torch.diag(1/(1.+torch.sum(A, dim=1)))
                A_tilde = D_tilde_inv @ (A + eye)
        return A_tilde 