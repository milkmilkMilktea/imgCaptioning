#using flickr 8k, flickr 30k, and COCO datasets
import os
import cv2
import csv
import numpy as np
from tqdm import tqdm
import matplotlib.pyplot as plt
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torchvision.models as models
from random import randint
import skimage
from skimage import io, transform
import math
import random
from pycocotools.coco import COCO
import string

class captionDataset(torch.utils.data.Dataset):
    vocab = []
    char_to_int = {}
    int_to_char = {}
    
    def __init__(self, root="data\\images\\", annotations="data\\captions.txt", img_size=299, word_level=False):
        self.root = root
        self.IMG_SIZE = img_size
        self.vocab = ["<PAD>", "<START>", "<END>", "\xa0"] #\xa0 is an "empty character". I put it here to fix an error
        
        with open(annotations, encoding='utf8') as file:
            csv_reader = np.array(list(csv.reader(file, delimiter="|"))) # gotta love mutability
        files, text = np.split(csv_reader[1:], 2, axis = 1)
        files = files.squeeze(1) #remove the leftover dimension from the split
        text = text.squeeze(1)
        
        if not word_level: #character level tokenization
            self.vocab += sorted(set(str(list(text))))
            self.maxlen = len(max(text, key=len))
        else: #word level tokenization
            self.maxlen = 0
            for i in text:
                for word in i.split(" "):
                    if word not in self.vocab:
                        self.vocab += [word]
                if len(i.split(" ")) > self.maxlen:
                    self.maxlen = len(i.split(" "))
                    
        self.maxlen += 2 # two more because of start and end tokens
                        
        self.char_to_int = dict((c, i) for i, c in enumerate(self.vocab))
        self.int_to_char = dict((i, c) for i, c in enumerate(self.vocab))
        
        captions = []
        for k in text:
            captions += [[1]] # 1 represents <START>
            
            captions[-1] += [self.char_to_int[i] for i in (k.split(" ") if word_level else k)]
            captions[-1] += [2] + [0]*(self.maxlen-len(captions[-1])-1) # 2 represents <END> and 0 is <PAD>
            
            
        
        self.X = files #X represents inputs
        self.y = captions #y represents labels
    
    
    def __len__(self):
        return len(self.y)
    
    def __getitem__(self, idx):
        img = io.imread(self.root+self.X[idx]) #using skimiage because its RGB and way faster than pillow
        img = cv2.resize(img,(self.IMG_SIZE,self.IMG_SIZE)).reshape(-1, self.IMG_SIZE, self.IMG_SIZE)/255.0
        caption = np.array(self.y[idx])
        return img.astype(np.float32), caption.astype(np.int64) #cast as float and long numpy arrays
    
class cocoDataset(torch.utils.data.Dataset):
    def __init__(self, root="data\\coco\\train2017\\", annotations="data\\coco\\annotations\\captions_train2017.json", img_size=299, word_level=False):
        self.root = root
        self.word_level = word_level
        self.IMG_SIZE = img_size
        
        self.coco = COCO(annotations)
        self.ids = sorted(list(self.coco.anns.keys()), key = (lambda z: self.coco.anns[z]['image_id']))
        
        #get vocab and maxlen here
        self.vocab = ["<PAD>", "<START>", "<END>", "\xa0"]
        new_vocab = set()
        self.maxlen = 0
        
        if word_level:
            for i in self.coco.anns.values():
                caption = list(filter(None, i["caption"].translate(str.maketrans('', '', string.punctuation+"\n")).split(" ")))
                for word in caption:
                    new_vocab.add(word)
                if len(caption) > self.maxlen:
                    self.maxlen = len(caption)
        else:
            for i in self.coco.anns.values():
                caption = i["caption"].translate(str.maketrans('', '', string.punctuation+"\n"))
                for char in caption:
                    new_vocab.add(char)
                if len(caption) > self.maxlen:
                    self.maxlen = len(caption)
        self.maxlen += 2
        
        self.vocab += sorted(new_vocab)
                
        
        self.char_to_int = dict((c, i) for i, c in enumerate(self.vocab))
        self.int_to_char = dict((i, c) for i, c in enumerate(self.vocab))
        
    def __len__(self):
        return len(self.ids)
    
    def __getitem__(self, idx):
        ID = self.ids[idx]
        caption = self.coco.anns[ID]['caption'].translate(str.maketrans('', '', string.punctuation+"\n'")) #this might be a bottleneck
        tokenized = [1] # 1 represents <START>
        tokenized += [self.char_to_int[i] for i in (list(filter(None, caption.split(" "))) if self.word_level else caption)]
        tokenized += [2] + [0]*(self.maxlen-len(tokenized)-1) # 2 represents <END> and 0 is <PAD>
        
        img_id = self.coco.anns[ID]['image_id']
        path = self.coco.loadImgs(img_id)[0]['file_name']
        img = io.imread(self.root+path) #using skimiage because its RGB and way faster than pillow
        if len(img.shape) == 2:
            img = skimage.color.gray2rgb(img)
        img = cv2.resize(img,(self.IMG_SIZE,self.IMG_SIZE)).reshape(3, self.IMG_SIZE, self.IMG_SIZE)/255.0
        
        return img.astype(np.float32), np.array(tokenized).astype(np.int64)
    
class train_val_test_split(torch.utils.data.Dataset):
    def __init__(self, dataset, split = (.8, .15, .05), mode="train"):
        self.dataset = dataset
        self.split = split
        self.mode = 0 if mode == "train" else 1 if mode == "val" else 2 if mode == "test" else -1
        self.interval = (round(sum(split[0:self.mode])*len(dataset)), round(sum(split[0:self.mode+1])*len(dataset)))
        
    def __len__(self):
        return round(len(self.dataset) * self.split[self.mode])
        
    def __getitem__(self, idx):
        # if idx+self.interval[0] >= self.interval[1]:
        #     raise StopIteration
        return self.dataset[idx+self.interval[0]]

class encoderCNN(nn.Module):
    def __init__(self):
        super(encoderCNN, self).__init__()
        self.num_features = 2048 #hyperparameter
        
        self.conv1 = nn.Conv2d(3, 128, 5, 1)
        self.conv1_bn=nn.BatchNorm2d(128)
        self.conv2 = nn.Conv2d(128, 512, 5, 1)
        self.conv2_bn=nn.BatchNorm2d(512)
        self.conv3 = nn.Conv2d(512, self.num_features, 3, 1)
        # self.conv3_bn=nn.BatchNorm2d(256)
        # self.conv4 = nn.Conv2d(256,1024, 3, 1)

    def convs(self, x):
        x = F.max_pool2d(F.relu(self.conv1_bn(self.conv1(x))), (4,4))
        x = F.max_pool2d(F.relu(self.conv2_bn(self.conv2(x))), (2,2))
        # x = F.max_pool2d(F.relu(self.conv3_bn(self.conv3(x))), (2,2))
        # x = self.conv4(x)
        x = self.conv3(x)
        return x

    def forward(self, x):
        features = self.convs(x)
        batch, feature_maps, size_1, size_2 = features.size()       
        features = features.permute(0, 2, 3, 1)
        features = features.view(batch, size_1*size_2, feature_maps)
        return features

class pretrained_encoderCNN(nn.Module):
    def __init__(self):
        super(pretrained_encoderCNN, self).__init__()
        self.num_features = 2048 #NOT hyperparameter
        
        resnet = models.resnet152(pretrained=True)
        for param in resnet.parameters():
            param.requires_grad = False
        
        modules = list(resnet.children())[:-2]
        self.resnet = nn.Sequential(*modules)
        
    def forward(self, images):
        features = self.resnet(images)
        batch, feature_maps, size_1, size_2 = features.size()       
        features = features.permute(0, 2, 3, 1)
        features = features.view(batch, size_1*size_2, feature_maps)
        return features


class BahdanauAttention(nn.Module):
    """ Class performs Additive Bahdanau Attention.
        Source: https://medium.com/analytics-vidhya/image-captioning-with-attention-part-1-e8a5f783f6d3
    """    
    def __init__(self, num_features, hidden_dim, attention_dim, output_dim = 1):
        super(BahdanauAttention, self).__init__()
        self.num_features = num_features
        self.hidden_dim = hidden_dim
        self.attention_dim = attention_dim
        self.output_dim = output_dim
        # fully-connected layer to learn first weight matrix Wa
        self.W_a = nn.Linear(self.num_features, self.attention_dim)
        # fully-connected layer to learn the second weight matrix Ua
        self.U_a = nn.Linear(self.hidden_dim, self.attention_dim)
        # fully-connected layer to produce score (output), learning weight matrix va
        self.v_a = nn.Linear(self.attention_dim, self.output_dim)
                
    def forward(self, features, decoder_hidden):
        """
        Arguments:
        ----------
        - features - features returned from Encoder
        - decoder_hidden - hidden state output from Decoder
                
        Returns:
        ---------
        - context - context vector with a size of (1,2048)
        - atten_weight - probabilities, express the feature relevance
        """
        # add additional dimension to a hidden (required for summation)
        decoder_hidden = decoder_hidden.unsqueeze(1)
        atten_1 = self.W_a(features)
        atten_2 = self.U_a(decoder_hidden)
        # apply tangent to combine result from 2 fc layers
        atten_tan = torch.tanh(atten_1+atten_2)
        atten_score = self.v_a(atten_tan)
        atten_weight = F.softmax(atten_score, dim = 1)
        # first, we will multiply each vector by its softmax score
        # next, we will sum up this vectors, producing the attention context vector
        # the size of context equals to a number of feature maps
        context = torch.sum(atten_weight * features,  dim = 1)
        atten_weight = atten_weight.squeeze(dim=2)
        
        return context, atten_weight    
    
class decoderRNN(nn.Module):
    def __init__(self, device, vocab_size, embedding_dim, hidden_size, dropout, force_temp, num_features, attention_dim):
        super(decoderRNN, self).__init__()
        self.vocab_size = vocab_size
        self.hidden_size = hidden_size #hidden state
        self.device = device
        self.force_temp = force_temp
        
        self.init_h = nn.Linear(num_features, hidden_size)
        self.init_c = nn.Linear(num_features, hidden_size)
        self.attention = BahdanauAttention(num_features, hidden_size, attention_dim)
        self.embed = nn.Embedding(vocab_size, embedding_dim)
        self.lstm = nn.LSTMCell(input_size=embedding_dim+num_features, hidden_size=hidden_size) #lstm
        self.dropout = nn.Dropout(dropout)
        self.fc = nn.Linear(hidden_size, vocab_size)
        
    
    def init_hidden(self, features): #small fully connected network to initialize states
        mean_features = torch.mean(features, dim = 1)
        h0 = self.init_h(mean_features)
        c0 = self.init_c(mean_features)
        return h0, c0
    
    def forward(self, features, captions, force_prob):
        embedding = self.embed(captions)
        h, c = self.init_hidden(features)
        seq_len = captions.size(1)
        batch_size = features.size(0)
        
        outputs = torch.zeros(batch_size, seq_len, self.vocab_size).to(self.device)
        atten_weights = torch.zeros(batch_size, seq_len, features.size(1)).to(self.device)
        
        for t in range(seq_len):
            use_sampling = False if t==0 else np.random.random() > force_prob
            if not use_sampling:
                word_embed = embedding[:,t,:]
            context, atten_weight = self.attention(features, h)
            input_concat = torch.cat([word_embed, context],dim=1)
            h, c = self.lstm(input_concat, (h,c))
            h = self.dropout(h)
            output = self.fc(h)
            if use_sampling:
                scaled_output = F.log_softmax(output, dim=1) / self.force_temp
                scoring = F.softmax(scaled_output, dim=1)
                top_idx = scoring.topk(1)[1]
                word_embed = self.embed(top_idx).squeeze(1)
            outputs[:, t, :] = output
            atten_weights[:, t, :] = atten_weight #Attention weights are here in case of future use
        return outputs
    

class CaptionNet(nn.Module):
    def __init__(self, device, vocab_size, embedding_dim, hidden_size, dropout, pretrained, word_level, force_temp, force_prob, attention_dim):
        super(CaptionNet, self).__init__()
        self.device = device
        self.word_level = word_level
        self.force_prob = force_prob
        
        
        self.encoder = pretrained_encoderCNN() if pretrained else encoderCNN()
        self.decoder = decoderRNN(device, vocab_size, embedding_dim, hidden_size, dropout, force_temp, self.encoder.num_features, attention_dim)
        
    def forward(self, images, captions):
        x = self.encoder(images)
        x = self.decoder(x, captions, self.force_prob)
        return x
    
    def caption(self, image, int_to_char, maxlen, temp = .2, beam_width = 1, beam_n = 1, verbose = 0):
        result = ""
        with torch.no_grad():
            features = self.encoder(torch.Tensor(image).unsqueeze(0).to(self.device))
            
            if beam_width <= 1 and beam_n <= 1:
                caption = torch.Tensor([[1]]).type(torch.LongTensor).to(self.device)
                
                for _ in range(maxlen):
                    output = self.decoder(features, caption, force_prob=1)
                    scaled_output = F.log_softmax(output.squeeze(0)[-1], dim=0) / temp
                    scoring = F.softmax(scaled_output, dim=0)
                    pred = scoring.topk(1)[1]
                    if int_to_char[pred.item()] == "<END>": #stop token
                        break

                    result += int_to_char[pred.item()] + (" " if self.word_level else "")
                    caption = torch.cat((caption, pred.unsqueeze(0)), dim=1)
            
            else: #Beam Search
                hypotheses = torch.Tensor([[1]]).type(torch.LongTensor).to(self.device)
                scores = [0]*beam_width
                
                completed = []
                while len(completed) < beam_n:
                    output = self.decoder(features.repeat(hypotheses.size(0), 1, 1), hypotheses, force_prob=1)[:,-1]
                    output = F.log_softmax(output, dim=1)
                    new_scores, tokens = output.topk(k = beam_width, dim = 1)

                    contenders = []
                    for i in range(hypotheses.size(0)):
                        for j in range(beam_width):
                            contenders += [(scores[i]+new_scores[i][j].item(), torch.cat((hypotheses[i], tokens[i][j].unsqueeze(0)), dim=0))]
                    contenders = sorted(contenders, key=(lambda y: y[0]), reverse = True)
                    if verbose == 1:
                        print(hypotheses, scores)
                        print(f"{new_scores}\n{tokens}")
                        print(contenders)
                    
                    moving_delimiter = 0
                    remove_list = []
                    for idx in range(len(contenders)):
                        if idx < beam_width + moving_delimiter and int_to_char[contenders[idx][1][-1].item()] == "<END>":
                            completed += [contenders[idx]] #add to output list
                            if verbose == 1:
                                print(f"COMPLETED {contenders[idx]}")
                            remove_list += [idx]
                            moving_delimiter += 1
                        elif int_to_char[contenders[idx][1][-1].item()] == "<END>":
                            remove_list += [idx]
                    for idx in sorted(remove_list, reverse = True):
                        del contenders[idx]
                    
                    if hypotheses.size(0) < beam_width:
                        for _ in range(beam_width - hypotheses.size(0)):
                            hypotheses = torch.cat((hypotheses, torch.Tensor([[0]*hypotheses.size(1)]).type(torch.LongTensor).to(self.device)),dim=0)
                    hypotheses = torch.cat((hypotheses, torch.Tensor([[0]]*hypotheses.size(0)).type(torch.LongTensor).to(self.device)),dim=1)
                    
                    for i in range(beam_width):
                        try:
                            scores[i] = contenders[i][0]
                            hypotheses[i] = contenders[i][1]
                        except:
                            scores[i] = contenders[0][0] #repopulate hypotheses with highest probability answers
                            hypotheses[i] = contenders[0][1]
                    if verbose == 1:
                        input("-"*35)
                    #print("="*45)
                if verbose == 1:
                    print(f"DONE!!! {sorted(completed, key=(lambda z: z[0]/z[1].size(0)), reverse = True)}")
                #normalize by length then pick the hypothesis with the highest score
                result = (" " if self.word_level else "").join([int_to_char[i.item()] for i in max(completed, key=(lambda z: z[0]/z[1].size(0)))[1] if i.item() not in [0,1,2]])
                
            
        return result.strip()
        
        
        
class captionGen: #wrapper object for easy training
    def __init__(self, img_size, big_data = 0, word_level = False):
        self.history = {"train_loss":[], "validation_loss":[]}
        self.WORD_LEVEL = word_level
        
        if big_data==2:
            self.dataset = cocoDataset(root="data\\coco\\train2017\\", annotations="data\\coco\\annotations\\captions_train2017.json", img_size=img_size, word_level=word_level)
        elif big_data==1:
            self.dataset = captionDataset(root="data\\big_images\\", annotations="data\\big_captions.txt", img_size=img_size, word_level=word_level)
        else: 
            self.dataset = captionDataset(root="data\\images\\", annotations="data\\captions.txt", img_size=img_size, word_level=word_level)
            
        self.train_data = train_val_test_split(self.dataset, split=(.8,.1,.1), mode = "train")
        self.val_data = train_val_test_split(self.dataset, split=(.8,.1,.1), mode = "val")
        self.test_data = train_val_test_split(self.dataset, split=(.8,.1,.1), mode = "test")
        print(f"{len(self.train_data)} training {len(self.val_data)} validation {len(self.test_data)} testing")
    
    def define(self, embedding_dim, hidden_size, attention_dim, dropout, force_temp, force_prob, pretrained):
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        
        self.net = CaptionNet(self.device, len(self.dataset.vocab), embedding_dim, hidden_size, dropout, pretrained, self.WORD_LEVEL, force_temp, force_prob, attention_dim).to(self.device)
        #print(f"\nUsing {self.device} device")
        if pretrained:
            print(self.net.decoder)
        else:
            print(self.net)
    
    def train(self, batch_size, epochs, lr, verbose):
        self.BATCH_SIZE = batch_size
        dataloader = torch.utils.data.DataLoader(self.train_data, batch_size=batch_size, shuffle=True)
        valdataloader = torch.utils.data.DataLoader(self.val_data, batch_size=batch_size, shuffle=True)
        
        optimizer = optim.Adam(filter(lambda p: p.requires_grad, self.net.parameters()), lr = lr) #filter to avoid passing in the pretrained encoder
        loss_criterion = nn.CrossEntropyLoss(ignore_index=self.dataset.char_to_int["<PAD>"])
        
        for epoch in range(epochs):
            self.net.train()
            loss_sum = 0
            for data in tqdm(dataloader, desc = f"Epoch {epoch+1}/{epochs} training") if verbose in [1,2] else dataloader:
                self.net.zero_grad()
                outputs = self.net(data[0].to(self.device), data[1].to(self.device)[:,:-1]) #input caption excludes last word
                loss = loss_criterion(outputs.view(-1,outputs.size(2)), data[1][:,1:].reshape(-1).to(self.device)) #target caption excludes first word
                loss_sum += loss.item()
                loss.backward()
                optimizer.step()
            if verbose in [1,2]:
                print(f"loss:\t{loss_sum/len(dataloader)}")
            self.history["train_loss"] += [loss_sum/len(dataloader)]
            
            self.net.eval()
            val_loss_sum = 0
            for val_data in tqdm(valdataloader, desc = "validating") if verbose in [1,2] else valdataloader:
                with torch.no_grad():
                    outputs = self.net(val_data[0].to(self.device), val_data[1].to(self.device)[:,:-1])
                    val_loss_sum += loss_criterion(outputs.view(-1,outputs.size(2)), val_data[1][:,1:].reshape(-1).to(self.device)).item()
            if verbose in [1,2]:
                print(f"val loss:\t{val_loss_sum/len(valdataloader)}")
            self.history["validation_loss"] += [val_loss_sum/len(valdataloader)]
            
            if verbose == 2:
                self.sample(1, .35)
    
    def sample(self, count = 1, temp = .2, beam_width = 1, beam_n = 1, verbose = 0):
        for curr_temp in (temp if type(temp) is list else [temp]):
            print(f"===========TEMP {curr_temp}===========" if beam_width <= 1 and beam_n <= 1 else f"===========BEAM===========")
            for _ in range(count):
                idx = random.randint(0,len(self.test_data)-1)
                plt.imshow(np.array(self.test_data[idx][0]*255).reshape(self.dataset.IMG_SIZE,self.dataset.IMG_SIZE,3).astype(np.uint8))
                plt.show()
                print(self.net.caption(self.test_data[idx][0], self.dataset.int_to_char, self.dataset.maxlen, curr_temp, beam_width, beam_n, verbose) + "\n")
    
    def curves(self):
        plt.xlabel("Epochs")
        plt.ylabel("Loss")
        plt.plot(self.history["train_loss"]) #blue is training loss
        plt.plot(self.history["validation_loss"]) #orange is validation loss
        plt.show()
