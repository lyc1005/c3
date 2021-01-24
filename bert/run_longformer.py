# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors and The HugginFace Inc. team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""BERT finetuning runner."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import csv
import os
import logging
import argparse
import random
from tqdm import tqdm, trange

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import TensorDataset, DataLoader, RandomSampler, SequentialSampler
from torch.utils.data.distributed import DistributedSampler

import tokenization
from transformers import BertTokenizer, BertModel, BertConfig
from longformer.longformer import *
from longformer.sliding_chunks import pad_to_window_size
from optimization import BERTAdam

import json

reverse_order = False
sa_step = False


logging.basicConfig(format = '%(asctime)s - %(levelname)s - %(name)s -   %(message)s', 
                    datefmt = '%m/%d/%Y %H:%M:%S',
                    level = logging.INFO)
logger = logging.getLogger(__name__)


class InputExample(object):
    """A single training/test example for simple sequence classification."""

    def __init__(self, guid, text_a, text_b=None, label=None, text_c=None):
        """Constructs a InputExample.

        Args:
            guid: Unique id for the example.
            text_a: string. The untokenized text of the first sequence. For single
            sequence tasks, only this sequence must be specified.
            text_b: (Optional) string. The untokenized text of the second sequence.
            Only must be specified for sequence pair tasks.
            label: (Optional) string. The label of the example. This should be
            specified for train and dev examples, but not for test examples.
        """
        self.guid = guid
        self.text_a = text_a
        self.text_b = text_b
        self.text_c = text_c
        self.label = label


class InputFeatures(object):
    """A single set of features of data."""

    def __init__(self, input_ids, input_mask, segment_ids, label_id):
        self.input_ids = input_ids
        self.input_mask = input_mask
        self.segment_ids = segment_ids
        self.label_id = label_id


class DataProcessor(object):
    """Base class for data converters for sequence classification data sets."""

    def get_train_examples(self, data_dir):
        """Gets a collection of `InputExample`s for the train set."""
        raise NotImplementedError()

    def get_dev_examples(self, data_dir):
        """Gets a collection of `InputExample`s for the dev set."""
        raise NotImplementedError()

    def get_labels(self):
        """Gets the list of labels for this data set."""
        raise NotImplementedError()

    @classmethod
    def _read_tsv(cls, input_file, quotechar=None):
        """Reads a tab separated value file."""
        with open(input_file, "r") as f:
            reader = csv.reader(f, delimiter="\t", quotechar=quotechar)
            lines = []
            for line in reader:
                lines.append(line)
            return lines


class c3Processor(DataProcessor):
    def __init__(self):
        random.seed(42)
        self.D = [[], [], []]
        self.opt_n = [[], [], []]

        for sid in range(3):
            data = []
            for subtask in ["d", "m"]:
                with open("data/c3-"+subtask+"-"+["train.json", "dev.json", "test.json"][sid], "r", encoding="utf8") as f:
                    data += json.load(f)
            if sid == 0:
                random.shuffle(data)
            for i in range(len(data)):
                for j in range(len(data[i][1])):
                    d = ['\n'.join(data[i][0]).lower(), data[i][1][j]["question"].lower()]
                    for k in range(len(data[i][1][j]["choice"])):
                        d += [data[i][1][j]["choice"][k].lower()]  
                    # for k in range(len(data[i][1][j]["choice"]), 4):
                        # d += ['']
                    d += [data[i][1][j]["answer"].lower()] 
                    self.D[sid] += [d]
                    self.opt_n[sid].append(len(data[i][1][j]["choice"]))
    
    def get_train_examples(self, data_dir):
        """See base class."""
        return self._create_examples(self.D[0], "train"), self.opt_n[0]

    def get_test_examples(self, data_dir):
        """See base class."""
        return self._create_examples(self.D[2], "test"), self.opt_n[2]

    def get_dev_examples(self, data_dir):
        """See base class."""
        return self._create_examples(self.D[1], "dev"), self.opt_n[1]

    def get_labels(self):
        """See base class."""
        return ["0", "1", "2", "3"]

    def _create_examples(self, data, set_type):
        """Creates examples for the training and dev sets."""
        examples = []
        for (i, d) in enumerate(data):
            for k in range(len(data[i])-3):
                if data[i][2+k] == data[i][-1]:
                    answer = k
                    
            # label = tokenization.convert_to_unicode(answer)

            for k in range(len(data[i])-3):
                guid = "%s-%s-%s" % (set_type, i, k)
                text_a = tokenization.convert_to_unicode(data[i][0])
                text_b = tokenization.convert_to_unicode(data[i][k+2])
                text_c = tokenization.convert_to_unicode(data[i][1])
                if k == answer:
                    label = '1'
                else:
                    label = '0'
                tokenization.convert_to_unicode(label)
                examples.append(
                        InputExample(guid=guid, text_a=text_a, text_b=text_b, label=label, text_c=text_c))
            
        return examples


class RCProcessor(DataProcessor):
    def __init__(self):
        random.seed(42)
        self.D = [[], [], []]
        self.opt_n = [[], [], []]
        self.ans_mapper = {'A':0,'B':1,'C':2,'D':3,'E':4}

        for sid in range(3):
            data = []
            with open("rc_data/"+["train_1.json", "dev_1.json", "test_1.json"][sid], "r", encoding="utf-8") as f:
                data += json.load(f)
            if sid == 0:
                random.shuffle(data)
            for p_idx, passage in enumerate(data):
                content = passage['Content']
                for q_idx, q in enumerate(passage['Questions']):
                    question = q['Question']
                    answer = q['Choices'][self.ans_mapper[q['Answer']]]
                    d = [content, question] + q['Choices'] + [answer]
                    self.D[sid].append(d)
                    self.opt_n[sid].append(len(q['Choices']))
    
    def get_train_examples(self, data_dir):
        """See base class."""
        return self._create_examples(self.D[0], "train"), self.opt_n[0]

    def get_test_examples(self, data_dir):
        """See base class."""
        return self._create_examples(self.D[2], "test"), self.opt_n[2]

    def get_dev_examples(self, data_dir):
        """See base class."""
        return self._create_examples(self.D[1], "dev"), self.opt_n[1]

    def get_labels(self):
        """See base class."""
        return ["0", "1", "2", "3", "4"]

    def _create_examples(self, data, set_type):
        """Creates examples for the training and dev sets."""
        examples = []
        for (i, d) in enumerate(data):
            for k in range(len(data[i])-3):
                if data[i][2+k] == data[i][-1]:
                    answer = k
                    
            # label = tokenization.convert_to_unicode(answer)

            for k in range(len(data[i])-3):
                guid = "%s-%s-%s" % (set_type, i, k)
                text_a = tokenization.convert_to_unicode(data[i][0])
                text_b = tokenization.convert_to_unicode(data[i][k+2])
                text_c = tokenization.convert_to_unicode(data[i][1])
                if k == answer:
                    label = '1'
                else:
                    label = '0'
                tokenization.convert_to_unicode(label)
                examples.append(
                        InputExample(guid=guid, text_a=text_a, text_b=text_b, label=label, text_c=text_c))
            
        return examples


def convert_examples_to_features(examples, label_list, max_seq_length, tokenizer):
    """Loads a data file into a list of `InputBatch`s."""

    print("#examples", len(examples))

    label_map = {}
    for (i, label) in enumerate(label_list):
        label_map[label] = i

    features = []
    for (ex_index, example) in enumerate(examples):
        tokens_a = tokenizer.tokenize(example.text_a)

        tokens_b = tokenizer.tokenize(example.text_b)

        tokens_c = tokenizer.tokenize(example.text_c)

        _truncate_seq_tuple(tokens_a, tokens_b, tokens_c, max_seq_length - 4)
        tokens_b = tokens_c + ["[SEP]"] + tokens_b

        tokens = []
        segment_ids = []
        input_mask = []
        tokens.append("[CLS]")
        segment_ids.append(0)
        input_mask.append(2)
        for token in tokens_a:
            tokens.append(token)
            segment_ids.append(0)
            input_mask.append(1)
        tokens.append("[SEP]")
        segment_ids.append(0)
        input_mask.append(1)

        if tokens_b:
            for token in tokens_b:
                tokens.append(token)
                segment_ids.append(1)
                input_mask.append(2)
            tokens.append("[SEP]")
            segment_ids.append(1)
            input_mask.append(2)

        input_ids = tokenizer.convert_tokens_to_ids(tokens)

        # The mask has 1 for real tokens and 0 for padding tokens. Only real
        # tokens are attended to.
        # input_mask = [1] * len(input_ids)

        # Zero-pad up to the sequence length.
        while len(input_ids) < max_seq_length:
            input_ids.append(0)
            input_mask.append(0)
            segment_ids.append(0)

        assert len(input_ids) == max_seq_length
        assert len(input_mask) == max_seq_length
        assert len(segment_ids) == max_seq_length

        label_id = label_map[example.label]
        if ex_index < 5:
            logger.info("*** Example ***")
            logger.info("guid: %s" % (example.guid))
            logger.info("tokens: %s" % " ".join(
                    [tokenization.printable_text(x) for x in tokens]))
            logger.info("input_ids: %s" % " ".join([str(x) for x in input_ids]))
            logger.info("input_mask: %s" % " ".join([str(x) for x in input_mask]))
            logger.info(
                    "segment_ids: %s" % " ".join([str(x) for x in segment_ids]))
            logger.info("label: %s (id = %d)" % (example.label, label_id))

        features.append(
                InputFeatures(
                        input_ids=input_ids,
                        input_mask=input_mask,
                        segment_ids=segment_ids,
                        label_id=label_id))

    print('#features', len(features))
    return features



def _truncate_seq_pair(tokens_a, tokens_b, max_length):
    """Truncates a sequence pair in place to the maximum length."""

    # This is a simple heuristic which will always truncate the longer sequence
    # one token at a time. This makes more sense than truncating an equal percent
    # of tokens from each, since if one sequence is very short then each token
    # that's truncated likely contains more information than a longer sequence.
    while True:
        total_length = len(tokens_a) + len(tokens_b)
        if total_length <= max_length:
            break
        if len(tokens_a) > len(tokens_b):
            tokens_a.pop()
        else:
            tokens_b.pop()


def _truncate_seq_tuple(tokens_a, tokens_b, tokens_c, max_length):
    """Truncates a sequence tuple in place to the maximum length."""

    # always to truncate tokens_a (the passage part in our case) 
    while True:
        total_length = len(tokens_a) + len(tokens_b) + len(tokens_c)
        if total_length <= max_length:
            break
        else:
            tokens_a.pop()            


def accuracy(preds, labels):
    assert len(preds) == len(labels)
    return np.sum(np.array(preds)==np.array(labels))/len(preds)


def construct_input_data(features):
    input_ids = []
    input_mask = []
    segment_ids = []
    label_id = []
    for f in features:
        input_ids.append(f.input_ids)
        input_mask.append(f.input_mask)
        segment_ids.append(f.segment_ids)
        label_id.append([f.label_id])                

    all_input_ids = torch.tensor(input_ids, dtype=torch.long)
    all_input_mask = torch.tensor(input_mask, dtype=torch.long)
    all_segment_ids = torch.tensor(segment_ids, dtype=torch.long)
    all_label_ids = torch.tensor(label_id, dtype=torch.long)
    return (all_input_ids, all_input_mask, all_segment_ids, all_label_ids)


def collapse_logits_to_answer(all_logits, all_label_ids, opt_n_ls):
    assert sum(opt_n_ls)==len(all_logits) and len(all_logits)==len(all_label_ids)
    preds, labels = [], []
    curr = 0
    for opt_n in opt_n_ls:
        logits = all_logits[curr : curr+opt_n]
        label_ids = all_label_ids[curr : curr+opt_n]
        assert sum(label_ids)==1
        curr += opt_n
        pred = np.argmax(logits)
        label = label_ids.index(1)
        preds.append(pred)
        labels.append(label)
    return preds, labels


def evaluate(model, dataloader, opt_n_ls, device, config=None, tokenizer=None):
    # if not next(model.parameters()).is_cuda:
    model.to(device)
    model.eval()
    eval_loss = 0
    nb_eval_steps, nb_eval_examples = 0, 0
    all_logits = []
    all_label_ids = []
    for input_ids, input_mask, segment_ids, label_ids in dataloader:
        input_ids = input_ids.to(device)
        input_mask = input_mask.to(device)
        label_ids = label_ids.to(device)
        input_ids, input_mask = pad_to_window_size(
                        input_ids, input_mask, config.attention_window[0], tokenizer.pad_token_id)
        with torch.no_grad():
            tmp_eval_loss, logits = model(input_ids=input_ids, 
                                          attention_mask=input_mask, 
                                          labels=label_ids)
            logits = F.softmax(logits, dim=1).detach().cpu().numpy()[:,1].tolist()
            label_ids = label_ids.view(-1).detach().cpu().numpy().tolist()
            all_logits.extend(logits)
            all_label_ids.extend(label_ids)

            eval_loss += tmp_eval_loss.mean().item()

            nb_eval_examples += input_ids.size(0)
            nb_eval_steps += 1

    eval_loss = eval_loss / nb_eval_steps

    preds, labels = collapse_logits_to_answer(all_logits, all_label_ids, opt_n_ls)
    eval_accuracy = accuracy(preds, labels)
    return eval_loss, eval_accuracy


class Model(nn.Module):
    def __init__(self, config, init_checkpoint):
        super(Model, self).__init__()
        self.encoder = Longformer.from_pretrained(init_checkpoint, config=config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, 2)

    def forward(self, input_ids, attention_mask, labels=None):
        _, pooler_output = self.encoder(input_ids=input_ids, 
                                        attention_mask=attention_mask)
        pooler_output = self.dropout(pooler_output)
        logits = self.classifier(pooler_output)
        if labels is not None:
            labels = labels.view(-1)
            loss = F.cross_entropy(logits, labels)
            return loss, logits
        else:
            return logits


def main():
    parser = argparse.ArgumentParser()

    ## Required parameters
    parser.add_argument("--data_dir",
                        default=None,
                        type=str,
                        required=True,
                        help="The input data dir. Should contain the .tsv files (or other data files) for the task.")
    parser.add_argument("--task_name",
                        default=None,
                        type=str,
                        required=True,
                        help="The name of the task to train.")
    parser.add_argument("--output_dir",
                        default=None,
                        type=str,
                        required=True,
                        help="The output directory where the model checkpoints will be written.")

    ## Other parameters
    parser.add_argument("--init_checkpoint",
                        default='schen/longformer-chinese-base-4096',
                        type=str,
                        help="Initial checkpoint key (search in https://huggingface.co/models)")
    parser.add_argument("--do_lower_case",
                        default=False,
                        action='store_true',
                        help="Whether to lower case the input text. True for uncased models, False for cased models.")
    parser.add_argument("--max_seq_length",
                        default=128,
                        type=int,
                        help="The maximum total input sequence length after WordPiece tokenization. \n"
                             "Sequences longer than this will be truncated, and sequences shorter \n"
                             "than this will be padded.")
    parser.add_argument("--do_train",
                        default=False,
                        action='store_true',
                        help="Whether to run training.")
    parser.add_argument("--do_eval",
                        default=False,
                        action='store_true',
                        help="Whether to run eval on the dev set.")
    parser.add_argument("--train_batch_size",
                        default=32,
                        type=int,
                        help="Total batch size for training.")
    parser.add_argument("--eval_batch_size",
                        default=8,
                        type=int,
                        help="Total batch size for eval.")
    parser.add_argument("--learning_rate",
                        default=5e-5,
                        type=float,
                        help="The initial learning rate for Adam.")
    parser.add_argument("--num_train_epochs",
                        default=3.0,
                        type=float,
                        help="Total number of training epochs to perform.")
    parser.add_argument("--warmup_proportion",
                        default=0.1,
                        type=float,
                        help="Proportion of training to perform linear learning rate warmup for. "
                             "E.g., 0.1 = 10%% of training.")
    parser.add_argument("--save_checkpoints_steps",
                        default=1000,
                        type=int,
                        help="How often to save the model checkpoint.")
    parser.add_argument("--no_cuda",
                        default=False,
                        action='store_true',
                        help="Whether not to use CUDA when available")
    parser.add_argument("--local_rank",
                        type=int,
                        default=-1,
                        help="local_rank for distributed training on gpus")
    parser.add_argument('--seed', 
                        type=int, 
                        default=42,
                        help="random seed for initialization")
    parser.add_argument('--gradient_accumulation_steps',
                        type=int,
                        default=1,
                        help="Number of updates steps to accumualte before performing a backward/update pass.")                       
    args = parser.parse_args()

    processors = {
        "c3": c3Processor,
        "rc": RCProcessor,
    }

    if args.local_rank == -1 or args.no_cuda:
        device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
        n_gpu = torch.cuda.device_count()
    else:
        device = torch.device("cuda", args.local_rank)
        n_gpu = 1
        # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
        torch.distributed.init_process_group(backend='nccl')
    logger.info("device %s n_gpu %d distributed training %r", device, n_gpu, bool(args.local_rank != -1))

    if args.gradient_accumulation_steps < 1:
        raise ValueError("Invalid gradient_accumulation_steps parameter: {}, should be >= 1".format(
                            args.gradient_accumulation_steps))

    args.train_batch_size = int(args.train_batch_size / args.gradient_accumulation_steps)

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)

    if not args.do_train and not args.do_eval:
        raise ValueError("At least one of `do_train` or `do_eval` must be True.")

    config = LongformerConfig.from_pretrained(args.init_checkpoint)

    if args.max_seq_length > config.max_position_embeddings:
        raise ValueError(
            "Cannot use sequence length {} because the BERT model was only trained up to sequence length {}".format(
            args.max_seq_length, config.max_position_embeddings))

    if os.path.exists(args.output_dir) and os.listdir(args.output_dir):
        if args.do_train:
            raise ValueError("Output directory ({}) already exists and is not empty.".format(args.output_dir))
    else:
        os.makedirs(args.output_dir, exist_ok=True)

    task_name = args.task_name.lower()

    if task_name not in processors:
        raise ValueError("Task not found: %s" % (task_name))

    processor = processors[task_name]()
    label_list = processor.get_labels()

    tokenizer = BertTokenizer.from_pretrained(args.init_checkpoint)

    train_examples = None
    num_train_steps = None
    if args.do_train:
        train_examples, train_opt_n = processor.get_train_examples(args.data_dir)
        num_train_steps = int(
            len(train_examples) / args.train_batch_size / args.gradient_accumulation_steps * args.num_train_epochs)

    model = Model(config, args.init_checkpoint)
    model.to(device)

    if args.local_rank != -1:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.local_rank],
                                                          output_device=args.local_rank)
    elif n_gpu > 1:
        model = torch.nn.DataParallel(model)

    no_decay = ['bias', 'gamma', 'beta']
    optimizer_parameters = [
        {'params': [p for n, p in model.named_parameters() if n not in no_decay], 'weight_decay_rate': 0.01},
        {'params': [p for n, p in model.named_parameters() if n in no_decay], 'weight_decay_rate': 0.0}
        ]

    optimizer = BERTAdam(optimizer_parameters,
                         lr=args.learning_rate,
                         warmup=args.warmup_proportion)

    global_step = 0

    if args.do_eval:
        eval_examples, dev_opt_n = processor.get_dev_examples(args.data_dir)
        eval_features = convert_examples_to_features(
            eval_examples, label_list, args.max_seq_length, tokenizer)

        eval_input_data = construct_input_data(eval_features)
        eval_data = TensorDataset(*eval_input_data)
        eval_dataloader = DataLoader(eval_data, batch_size=args.eval_batch_size)

    
    if args.do_train:
        best_accuracy = 0
        
        train_features = convert_examples_to_features(
            train_examples, label_list, args.max_seq_length, tokenizer)
        logger.info("***** Running training *****")
        logger.info("  Num examples = %d", len(train_examples))
        logger.info("  Batch size = %d", args.train_batch_size)
        logger.info("  Num steps = %d", num_train_steps)
        
        train_input_data = construct_input_data(train_features)
        train_data = TensorDataset(*train_input_data)
        
        if args.local_rank == -1:
            train_sampler = RandomSampler(train_data)
        else:
            train_sampler = DistributedSampler(train_data)
        train_dataloader = DataLoader(train_data, sampler=train_sampler, batch_size=args.train_batch_size)

        # 开始训练
        for _ in trange(int(args.num_train_epochs), desc="Epoch"):
            model.train()
            tr_loss = 0
            nb_tr_examples, nb_tr_steps = 0, 0
            for step, batch in enumerate(tqdm(train_dataloader, desc="Iteration")):
                if step>1000:
                    continue
                batch = tuple(t.to(device) for t in batch)
                input_ids, input_mask, segment_ids, label_ids = batch
                input_ids, input_mask = pad_to_window_size(
                input_ids, input_mask, config.attention_window[0], tokenizer.pad_token_id)
                loss, _ = model(input_ids=input_ids, 
                                attention_mask=input_mask, 
                                labels=label_ids)
                if n_gpu > 1:
                    loss = loss.mean() # mean() to average on multi-gpu.
                if args.gradient_accumulation_steps > 1:
                    loss = loss / args.gradient_accumulation_steps
                if step%100==0:
                    logger.info("  loss = %f", loss)
                loss.backward()
                tr_loss += loss.item()
                nb_tr_examples += input_ids.size(0)
                nb_tr_steps += 1
                if (step + 1) % args.gradient_accumulation_steps == 0:
                    optimizer.step()    # We have accumulated enought gradients
                    model.zero_grad()
                    global_step += 1
            # 每个epoch结束评估验证集
            eval_loss, eval_accuracy = evaluate(model, eval_dataloader, dev_opt_n, device, config=config, tokenizer=tokenizer)

            if args.do_train:
                result = {'eval_loss': eval_loss,
                          'eval_accuracy': eval_accuracy,
                          'global_step': global_step,
                          'loss': tr_loss/nb_tr_steps}
            else:
                result = {'eval_loss': eval_loss,
                          'eval_accuracy': eval_accuracy}

            logger.info("***** Eval results *****")
            for key in sorted(result.keys()):
                logger.info("  %s = %s", key, str(result[key]))

            if eval_accuracy >= best_accuracy:
                torch.save(model.state_dict(), os.path.join(args.output_dir, "model_best.pt"))
                best_accuracy = eval_accuracy
                
        model.load_state_dict(torch.load(os.path.join(args.output_dir, "model_best.pt")))
        torch.save(model.state_dict(), os.path.join(args.output_dir, "model.pt"))
  
    #训练结束
    #开始评估
    model.load_state_dict(torch.load(os.path.join(args.output_dir, "model.pt")))
    if args.do_eval:
        logger.info("***** Running evaluation *****")
        logger.info("  Num examples = %d", len(eval_examples))
        logger.info("  Batch size = %d", args.eval_batch_size)
        
        eval_loss, eval_accuracy = evaluate(model, eval_dataloader, dev_opt_n, device, config=config, tokenizer=tokenizer)

        if args.do_train:
            result = {'eval_loss': eval_loss,
                      'eval_accuracy': eval_accuracy,
                      'global_step': global_step,
                      'loss': tr_loss/nb_tr_steps}
        else:
            result = {'eval_loss': eval_loss,
                      'eval_accuracy': eval_accuracy}

        output_eval_file = os.path.join(args.output_dir, "eval_results_dev.txt")
        with open(output_eval_file, "w") as writer:
            logger.info("***** Eval results *****")
            for key in sorted(result.keys()):
                logger.info("  %s = %s", key, str(result[key]))
                writer.write("%s = %s\n" % (key, str(result[key])))
        # output_eval_file = os.path.join(args.output_dir, "logits_dev.txt")
        # with open(output_eval_file, "w") as f:
        #     for i in range(len(logits_all)):
        #         for j in range(len(logits_all[i])):
        #             f.write(str(logits_all[i][j]))
        #             if j == len(logits_all[i])-1:
        #                 f.write("\n")
        #             else:
        #                 f.write(" ")

        # 评估测试集
        test_examples, test_opt_n = processor.get_test_examples(args.data_dir)
        test_features = convert_examples_to_features(
            test_examples, label_list, args.max_seq_length, tokenizer)

        logger.info("***** Running evaluation *****")
        logger.info("  Num examples = %d", len(test_examples))
        logger.info("  Batch size = %d", args.eval_batch_size)
        
        test_input_data = construct_input_data(test_features)
        test_data = TensorDataset(*test_input_data)
        
        test_dataloader = DataLoader(test_data, batch_size=args.eval_batch_size)

        test_loss, test_accuracy = evaluate(model, test_dataloader, test_opt_n, device, config=config, tokenizer=tokenizer)

        if args.do_train:
            result = {'eval_loss': test_loss,
                      'eval_accuracy': test_accuracy,
                      'global_step': global_step,
                      'loss': tr_loss/nb_tr_steps}
        else:
            result = {'eval_loss': test_loss,
                      'eval_accuracy': test_accuracy}

        output_eval_file = os.path.join(args.output_dir, "eval_results_test.txt")
        with open(output_eval_file, "w") as writer:
            logger.info("***** Eval results *****")
            for key in sorted(result.keys()):
                logger.info("  %s = %s", key, str(result[key]))
                writer.write("%s = %s\n" % (key, str(result[key])))
        # output_eval_file = os.path.join(args.output_dir, "logits_test.txt")
        # with open(output_eval_file, "w") as f:
        #     for i in range(len(logits_all)):
        #         for j in range(len(logits_all[i])):
        #             f.write(str(logits_all[i][j]))
        #             if j == len(logits_all[i])-1:
        #                 f.write("\n")
        #             else:
        #                 f.write(" ")

if __name__ == "__main__":
    main()
