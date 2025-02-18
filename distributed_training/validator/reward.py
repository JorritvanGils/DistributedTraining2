# The MIT License (MIT)
# Copyright © 2023 Yuma Rao
# Copyright © 2023 KMFODA

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

import asyncio
import random
import time
from typing import List

import base58
import bittensor as bt
import numpy as np
import torch
import torch.nn.functional as F
from distributed_training.data.dataset import DatasetLoader
from distributed_training.utils.state_loader import cleanup_old_cache
from distributed_training.utils.uids import (
    get_random_uids,
    map_uid_to_peerid,
    update_run_peerid_list,
)
from hivemind.p2p import PeerID
from huggingface_hub import list_repo_commits
from transformers import AutoModelForCausalLM

# GPU optimizations.
torch.backends.cudnn.benchmark = True
torch.backends.cuda.matmul.allow_tf32 = True
torch.backends.cudnn.allow_tf32 = True

# Seeds
torch.manual_seed(42)
torch.cuda.manual_seed(42)


async def score_blacklist(self, uids):
    scores = torch.FloatTensor([1 for _ in uids]).to(self.device)
    for i, uid in enumerate(uids):
        if self.uids_to_peerids[uid][0] == None:
            scores[i] = 0.0
        elif self.uids_to_peerids[uid][0] in self.run_peer_id_list:
            scores[i] = 1.0
        else:
            scores[i] = 0.0

    return scores


async def score_bandwidth(self, uids, timeout=30):
    scores = torch.FloatTensor([1 for _ in uids]).to(self.device)
    for i, uid in enumerate(uids):
        peer_id = self.uids_to_peerids[uid][0]

        if peer_id is None:
            peer = None
        else:
            peer = PeerID(base58.b58decode(peer_id))

        if peer is None:
            scores[i] = 0

        else:
            try:
                start_time = time.perf_counter()

                metadata, tensors = await asyncio.wait_for(
                    self.load_state_from_miner(peer), timeout=timeout
                )
                end_time = time.perf_counter()

                if (metadata is None) or (tensors is None):
                    scores[i] = 0
                else:
                    scores[i] = 1 - ((end_time - start_time) / timeout)

                bt.logging.info(f"Reward for peer {peer} is {scores[i]}")

            except Exception as e:
                bt.logging.info(f"Failed to download state from {peer} - {repr(e)}")
                scores[i] = 0
                bt.logging.info(f"Reward for peer {peer} is {scores[i]}")

    return scores


def score_failed_senders(self, uids, failed_peers, participating_peers):
    scores = torch.FloatTensor([0.0 for _ in uids]).to(self.device)
    for i, uid in enumerate(uids):
        peer_id = self.uids_to_peerids.get(uid)[0]

        if peer_id in participating_peers:
            if peer_id in failed_peers:
                bt.logging.info(f"UID:{uid} - Failed participating peer")
                scores[i] = 0.0
            else:
                bt.logging.info(f"UID:{uid} - Successful participating peer")
                scores[i] = 1.0
        else:
            bt.logging.info(f"UID:{uid} - Non participating peer")
            scores[i] = 0.0

    return scores


async def fetch_training_data(self, block):
    """Async function to fetch training data"""

    try:
        pages = await DatasetLoader.next_pages(
            offset=block,
            n_pages=5,
            seed=self.uid if not self.config.random else random.randint(0, 1000),
        )
        random.shuffle(pages)

        dataset = await DatasetLoader.create(
            batch_size=self.config.neuron.local_batch_size_train,
            sequence_length=1024,
            pages_info=pages,
            tokenizer=self.tokenizer,
        )

        return dataset
    except Exception as e:
        bt.logging.error(f"Error fetching training data: {str(e)}")
        raise


async def score_uid(self, uid: int):
    """Score a single UID"""

    if self.uid_tracker[uid]["model_huggingface_id"] is None:
        return 0

    cleanup_old_cache(
        self,
        repo_id=self.uid_tracker[uid]["model_huggingface_id"],
        current_revision=None,
    )

    commits = list_repo_commits(
        self.uid_tracker[uid]["model_huggingface_id"], repo_type="model"
    )[:2]
    latest_commit = commits[0].commit_id
    time_delta = (commits[0].created_at - commits[1].created_at).seconds

    model_huggingface_id = self.uid_tracker[uid]["model_huggingface_id"]

    self.model = AutoModelForCausalLM.from_pretrained(
        model_huggingface_id, revision=commits[0].commit_id, trust_remote_code=True
    )
    # Move the model to the appropriate device
    self.model = self.model.to(self.device)

    model_final = AutoModelForCausalLM.from_pretrained(
        model_huggingface_id, revision=commits[1].commit_id, trust_remote_code=True
    )

    blocks = model_final.config.block_list
    try:
        for block in blocks:
            dataset = await fetch_training_data(self, block)
            total_loss = 0
            batch_count = 0
            inner_step_counter = 0

            for inputs, labels in dataset:
                # Move to device
                inputs, labels = inputs.to(self.device), labels.to(self.device)

                with torch.autocast(device_type="cuda", dtype=torch.bfloat16):
                    outputs = self.model(input_ids=inputs, labels=labels)
                    loss = outputs[1]

                loss.backward()

                self.local_progress.samples_accumulated += inputs.size(0)
                total_loss += loss.detach().item()
                batch_count += 1
                inner_step_counter += 1

                if batch_count % 5 == 0:
                    bt.logging.info(
                        f":training: Inner Step: {inner_step_counter} | Average Loss: {total_loss / batch_count:.4f}"
                    )

                self.inner_optimizer.step()
                self.inner_optimizer.zero_grad()

    except Exception:
        bt.logging.error("Forward Loop Failed, Falling Back To Full Reward")
        return torch.tensor([1.0])

    cleanup_old_cache(
        self,
        repo_id=model_huggingface_id,
        current_revision=None,
    )

    try:
        rewards = score_models(self.model, model_final)
    except Exception as e:
        bt.logging.error(f"Error calculating final score: {str(e)}")
        rewards = 1.0

    return rewards, latest_commit, time_delta, blocks


def score_models(model_1, model_2):
    """Calculate the cosine similarity score between two model sates"""
    score = 0
    index = 0

    for param_1, param_2 in zip(model_1.parameters(), model_2.parameters()):
        score += (
            F.cosine_similarity(param_1.to("cpu"), param_2.to("cpu"), dim=0)
            .mean()
            .item()
        )
        index += 1

    average_score = score / index
    return average_score
