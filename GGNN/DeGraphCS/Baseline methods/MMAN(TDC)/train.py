import os
os.environ["KMP_DUPLICATE_LIB_OK"]="TRUE"
import sys
import random
import time
from datetime import datetime
import numpy as np
import math
import argparse
random.seed(42)
from tqdm import tqdm

import logging
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s")

import torch

import models, configs, data_loader 
from modules import get_cosine_schedule_with_warmup
from utils import similarity, normalize
from data_loader import *

    
def train(args):
    fh = logging.FileHandler(f"./output/{args.model}/{args.dataset}/logs.txt")
                                      # create file handler which logs even debug messages
    logger.addHandler(fh)# add the handlers to the logger
    timestamp = datetime.now().strftime('%Y%m%d%H%M') 
    
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed(args.seed)
    device = torch.device(f"cuda:{args.gpu_id}" if torch.cuda.is_available() else "cpu") 
    
    def save_model(model, epoch):
        torch.save(model.state_dict(), f'./output/{args.model}/{args.dataset}/models/epo{epoch}.h5')

    def load_model(model, epoch, to_device):
        assert os.path.exists(f'./output/{args.model}/{args.dataset}/models/epo{epoch}.h5'), f'Weights at epoch {epoch} not found'
        model.load_state_dict(torch.load(f'./output/{args.model}/{args.dataset}/models/epo{epoch}.h5', map_location=to_device))

    config = getattr(configs, 'config_'+args.model)()
    print(config)
    
    ###############################################################################
    # Load data
    ###############################################################################
    data_path = args.data_path+args.dataset+'/'
    train_set = eval(config['dataset_name'])(config, data_path, 
                                  config['train_token'], config['tok_len'],
                                  config['train_dfg'], config['n_node'],
                                  config['train_cfg'], config['n_node'],
                                  config['train_desc'], config['desc_len'])
    '''
    valid_set = eval(config['dataset_name'])(data_path,
                                  config['valid_tokens'], config['tokens_len'],
                                  config['valid_desc'], config['desc_len'])
    '''
    data_loader = torch.utils.data.DataLoader(dataset=train_set, batch_size=config['batch_size'], 
                                       shuffle=True, drop_last=False, num_workers=1)
    
    ###############################################################################
    # Define the models
    ###############################################################################
    logger.info('Constructing Model..')
    model = getattr(models, args.model)(config) #initialize the model
    if args.reload_from>0:
        load_model(model, args.reload_from, device)    
    logger.info('done')
    model.to(device)

    no_decay = ['bias', 'LayerNorm.weight']
    optimizer_grouped_parameters = [
            {'params': [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)], 'weight_decay': 0.01},
            {'params': [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
    ]    
    # origin: AdamW
    optimizer = torch.optim.AdamW(optimizer_grouped_parameters, lr=config['learning_rate'], eps=config['adam_epsilon'])        
    # no scheduler in paper's original code
    scheduler = get_cosine_schedule_with_warmup(
            optimizer, num_warmup_steps=config['warmup_steps'], 
            num_training_steps=len(data_loader)*config['nb_epoch']) # do not foget to modify the number when dataset is changed
    if config['fp16']:
        try:
            from apex import amp
        except ImportError:
            raise ImportError("Please install apex from https://www.github.com/nvidia/apex to use fp16 training.")
        model, optimizer = amp.initialize(model, optimizer, opt_level=config['fp16_opt_level'])
    
    print('---model parameters---')
    num_params = 0
    for param in model.parameters():
        num_params += param.numel()
    print(num_params / 1e6)


    n_iters = len(data_loader)
    itr_global = args.reload_from+1 
    for epoch in range(int(args.reload_from)+1, config['nb_epoch']+1): 
        itr_start_time = time.time()
        losses=[]
        for batch in data_loader:
            
            model.train()
            batch_gpu = [tensor.to(device) for tensor in batch]
            loss = model(*batch_gpu)
            
            if config['fp16']:
                with amp.scale_loss(loss, optimizer) as scaled_loss:
                    scaled_loss.backward()
                torch.nn.utils.clip_grad_norm_(amp.master_params(optimizer), 5.0)
            else:
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
                
            optimizer.step()
            scheduler.step()
            model.zero_grad()
            
            losses.append(loss.item())
            
            if itr_global % args.log_every == 0:
                elapsed = time.time() - itr_start_time
                logger.info('epo:[%d/%d] itr:[%d/%d] step_time:%ds Loss=%.5f'%
                        (epoch, config['nb_epoch'], itr_global%n_iters, n_iters, elapsed, np.mean(losses)))
                    
                losses=[] 
                itr_start_time = time.time() 
            itr_global = itr_global + 1

       
        # save every epoch
        if epoch >= 90:
            if epoch % 5 == 0:
                save_model(model, epoch)

##### Evaluation #####
def validate(valid_set, model, pool_size, K, sim_measure):
    """
    simple validation in a code pool. 
    @param: poolsize - size of the code pool, if -1, load the whole test set
    """
    def ACC(real,predict):
        sum=0.0
        for val in real:
            try: index=predict.index(val)
            except ValueError: index=-1
            if index!=-1: sum=sum+1  
        return sum/float(len(real))
    def MAP(real,predict):
        sum=0.0
        for id,val in enumerate(real):
            try: index=predict.index(val)
            except ValueError: index=-1
            if index!=-1: sum=sum+(id+1)/float(index+1)
        return sum/float(len(real))
    def MRR(real,predict):
        sum=0.0
        for val in real:
            try: index=predict.index(val)
            except ValueError: index=-1
            if index!=-1: sum=sum+1.0/float(index+1)
        return sum/float(len(real))
    def NDCG(real,predict):
        dcg=0.0
        idcg=IDCG(len(real))
        for i,predictItem in enumerate(predict):
            if predictItem in real:
                itemRelevance=1
                rank = i+1
                dcg+=(math.pow(2,itemRelevance)-1.0)*(math.log(2)/math.log(rank+1))
        return dcg/float(idcg)
    def IDCG(n):
        idcg=0
        itemRelevance=1
        for i in range(n): idcg+=(math.pow(2,itemRelevance)-1.0)*(math.log(2)/math.log(i+2))
        return idcg

    model.eval()
    device = next(model.parameters()).device

    data_loader = torch.utils.data.DataLoader(dataset=valid_set, batch_size=pool_size, 
                                 shuffle=True, drop_last=True, num_workers=1) # batch_size=10000
    accs, mrrs, maps, ndcgs=[],[],[],[]
    code_reprs, desc_reprs = [], []
    n_processed = 0
    for batch in tqdm(data_loader):        
        if len(batch) == 6: # toks, tok_len, descs, desc_len, bad_descs, bad_desc_len
            code_batch = [tensor.to(device) for tensor in batch[:2]]
            desc_batch = [tensor.to(device) for tensor in batch[2:4]]
        elif len(batch) == 4:
            code_batch = [tensor.to(device) for tensor in batch[:2]]
            desc_batch = [tensor.to(device) for tensor in batch[2:4]]
        #else: # code_ids, type_ids, code_mask, good_ids, good_mask, bad_ids, bad_mask
            #code_batch = [tensor.to(device) for tensor in batch[:3]]
            #desc_batch = [tensor.to(device) for tensor in batch[3:5]]
            #assert(False, 'something wrong in the valid dataloader.')
        with torch.no_grad():
            code_repr = model.code_encoding(*code_batch).data.cpu().numpy().astype(np.float32)
            desc_repr = model.desc_encoding(*desc_batch).data.cpu().numpy().astype(np.float32) # [poolsize x hid_size]
            if sim_measure=='cos':
                code_repr = normalize(code_repr)
                desc_repr = normalize(desc_repr)
        code_reprs.append(code_repr)
        desc_reprs.append(desc_repr)
        n_processed += batch[0].size(0) # batch_size
    code_reprs, desc_reprs = np.vstack(code_reprs), np.vstack(desc_reprs)
     
    for k in tqdm(range(0, n_processed, pool_size)):
        code_pool, desc_pool = code_reprs[k:k+pool_size], desc_reprs[k:k+pool_size] 
        for i in range(min(10000, pool_size)): # for i in range(pool_size):
            desc_vec = np.expand_dims(desc_pool[i], axis=0) # [1 x dim]
            n_results = K    
            if sim_measure=='cos':
                sims = np.dot(code_pool, desc_vec.T)[:,0] # [pool_size]
            else:
                sims = similarity(code_pool, desc_vec, sim_measure) # [pool_size]
                
            negsims = np.negative(sims)
            predict = np.argpartition(negsims, kth=n_results-1)#predict=np.argsort(negsims)#
            predict = predict[:n_results]   
            predict = [int(k) for k in predict]
            real = [i]
            accs.append(ACC(real,predict))
            mrrs.append(MRR(real,predict))
            maps.append(MAP(real,predict))
            ndcgs.append(NDCG(real,predict))                     
    return np.mean(accs),np.mean(mrrs),np.mean(maps),np.mean(ndcgs)
   
    
def parse_args():
    parser = argparse.ArgumentParser("Train and Validate The Code Search (Embedding) Model")
    parser.add_argument('--data_path', type=str, default='./data/', help='location of the data corpus')
    parser.add_argument('--model', type=str, default='CFGEmbeder', help='model name')
    parser.add_argument('--dataset', type=str, default='github11', help='name of dataset.java, python')
    parser.add_argument('--reload_from', type=int, default=-1, help='epoch to reload from')
   
    parser.add_argument('-g', '--gpu_id', type=int, default=0, help='GPU ID')
    parser.add_argument('-v', "--visual",action="store_true", default=False, help="Visualize training status in tensorboard")
    # Training Arguments
    parser.add_argument('--log_every', type=int, default=50, help='interval to log autoencoder training results')
    parser.add_argument('--valid_every', type=int, default=5000, help='interval to validation')
    parser.add_argument('--save_every', type=int, default=10000, help='interval to evaluation to concrete results')
    parser.add_argument('--seed', type=int, default=1111, help='random seed')
        
    parser.add_argument("--local_rank", type=int, default=-1, help="For distributed training: local_rank")
    
    return parser.parse_args()

if __name__ == '__main__':
    args = parse_args()
    
    # make output directory if it doesn't already exist
    os.makedirs(f'./output/{args.model}/{args.dataset}/models', exist_ok=True)
    os.makedirs(f'./output/{args.model}/{args.dataset}/tmp_results', exist_ok=True)
    
    torch.backends.cudnn.benchmark = True # speed up training by using cudnn
    torch.backends.cudnn.deterministic = True # fix the random seed in cudnn
   
    train(args)
        
    