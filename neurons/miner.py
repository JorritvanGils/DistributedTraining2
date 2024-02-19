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

import re
import time
import typing
import random
from ipaddress import ip_address

import bittensor as bt
import hivemind
import requests
import torch
import wandb

from hivemind import utils
from hivemind.optim.progress_tracker import ProgressTracker
from transformers import AutoModelForCausalLM
    
# Bittensor Miner Template:
import template
from bitarray import bitarray

# import base miner class which takes care of most of the boilerplate
from template.base.miner import BaseMinerNeuron
from template.utils.misc import load_wandb, setup_logging, get_bandwidth, DTGradientAverager, init_dht
from template.data.dataset import SubsetFalconLoader


class Miner(BaseMinerNeuron):
    def __init__(self, config=None):
        super(Miner, self).__init__(config=config)

        # Init DHT
        init_dht(self)
        
        # Init device
        self.device = self.config.neuron.device

        # Init Model
        self.model = AutoModelForCausalLM.from_pretrained(self.config.neuron.model_name)

        # Move the model to the appropriate device
        self.model = self.model.to(self.device)

        # Set up a decentralized optimizer that will average with peers in background
        self.opt = torch.optim.AdamW(self.model.parameters(), lr=self.config.neuron.lr)

        self.grad_averager = DTGradientAverager(
            self.model.parameters(),
            dht=self.dht,
            prefix=f"{self.config.neuron.run_id}_grad_averager",
            compression=hivemind.Uniform8BitQuantization(),
            # reuse_grad_buffers=True,
            accumulate_grads_on=torch.device(self.device),
            start = True
        )

        self.tracker = ProgressTracker(
            dht=self.dht, 
            prefix=f"{self.config.neuron.run_id}_progress", 
            target_batch_size=self.config.neuron.global_batch_size_train,
            start=True
        )
        self.step_scheduled = False
        self.local_epoch, self.local_samples = 0, 0

        # Load dataset
        self.dataset_loader = ()
        dataset_length = 968000015
        self.dataset_indices = bitarray(dataset_length)

        # Init Wandb
        if not self.config.neuron.dont_wandb_log:
            self.wandb = load_wandb(self.config, self.wallet, "miner", str(1))

    def get_miner_info(self):
        return {
            "block": self.metagraph.block.item(),
            "stake": self.metagraph.stake[self.uid],
            "trust": self.metagraph.trust[self.uid],
            "consensus": self.metagraph.consensus[self.uid],
            "incentive": self.metagraph.incentive[self.uid],
            "emissions": self.metagraph.emission[self.uid],
        }

    async def is_alive(
        self, synapse: template.protocol.IsAlive
    ) -> template.protocol.IsAlive:
        bt.logging.info("Responded to be Active")
        synapse.completion = "True"
        return synapse

    async def all_reduce(
        self, synapse: template.protocol.AllReduce
    ) -> template.protocol.IsAlive:
        
        bt.logging.info("Received All Reduce Call")

        # Aggregate gradients and perform optimizer step when target batch size is reached
        with self.tracker.pause_updates():
            bt.logging.info("Performing Gradient Averaging")
            self.grad_averager.step(timeout = (synapse.timeout))
            with self.grad_averager.use_averaged_gradients():  # this will fill param.grads with aggregated gradients
                bt.logging.info("Performing Optimizer Step")
                self.opt.step()  # update model parameters using averaged gradients
            self.grad_averager.reset_accumulated_grads_()  # prepare for next step
            self.local_epoch = self.tracker.update_epoch(self.local_epoch + 1)
            self.local_samples = 0  

        synapse.completion = "True"
        return synapse

    async def forward(
        self, synapse: template.protocol.Train
    ) -> template.protocol.Train:
        """
        Processes the incoming 'Train' synapse by performing a training run

        Args:
            synapse (template.protocol.Train): The synapse object containing the 'dataset_indices' data.

        Returns:
            template.protocol.Train: The synapse object with the 'loss' field set to models loss.
        """
       
        search_start = random.choice(range(len(self.dataset_indices) -  self.config.neuron.training_examples_per_miner + 1))
        start = self.dataset_indices.index(bitarray('0'* self.config.neuron.training_examples_per_miner), search_start)
        group = [i for i in range(start,start +  self.config.neuron.training_examples_per_miner)]
        self.dataset_indices[group] = True

        # Create Dataloader
        dataloader = SubsetFalconLoader(
            batch_size=self.config.neuron.local_batch_size_train, sequence_length=1024, rows=group
        )

        total_loss = 0
        # Train data for one epoch
        for index, batch in enumerate(dataloader):
            inputs = batch.to(self.device)

            # Forward pass
            outputs = self.model(input_ids=inputs, labels=inputs)

            # Normalize loss to account for batch accumulation
            loss = outputs.loss
            
            # Accumulate Total Loss
            total_loss += outputs.loss.detach().item() 

            # Backward Pass
            loss.backward()
            
            # Copy gradients
            gradients = tuple(param.grad.detach().cpu().clone() if param.grad is not None else torch.zeros_like(param) for param in self.model.parameters())

            # Accumulate Gradients
            self.grad_averager.accumulate_grads_(batch_size=len(inputs))
            
            # Zero Gradients
            self.opt.zero_grad()

            # Update Tracker
            self.local_samples += 1    
            self.tracker.report_local_progress(self.local_epoch, self.local_samples)

            # Log accumulation status
            if index % 10 == 0:
                bt.logging.info(f"Local samples: {self.local_samples} | Local epoch: {self.local_epoch} | Loss: {outputs.loss.detach().item():.2f}")
                bt.logging.info(f"Global samples: {self.tracker.global_progress.samples_accumulated} | Global epoch: {self.tracker.global_progress.epoch} | Number of Peers: {self.tracker.global_progress.num_peers}")

            if not self.config.neuron.dont_wandb_log:
                self.wandb.log({"loss": outputs.loss.detach().item(), "local_epoch": self.local_epoch, "global_epoch": self.tracker.global_progress.epoch})
        
        if index % 10 != 0:
            bt.logging.info(f"Local samples: {self.local_samples} | Local epoch: {self.local_epoch} | Loss: {outputs.loss.detach().item():.2f}")
            bt.logging.info(f"Global samples: {self.tracker.global_progress.samples_accumulated} | Global epoch: {self.tracker.global_progress.epoch} | Number of Peers: {self.tracker.global_progress.num_peers}")

        # Store summed random gradients in the synapse
        synapse.gradients = float(sum(gradients[synapse.gradient_test_index]))

        average_loss = total_loss / index
        synapse.loss = average_loss
        synapse.dataset_indices = group

        event = {}
        event.update(self.get_miner_info())
        try:
            event.update(get_bandwidth())
        except:
            bt.logging.info("Error getting bandwidth metrics")
        event.update({'steps':index})
        
        # bt.logging.debug(f"Events: {str(event)}")
        # bt.logging.info("EVENTS", "events", **event)

        if not self.config.neuron.dont_wandb_log:
            self.wandb.log(event)

        return synapse

    async def blacklist_base(self, synapse) -> typing.Tuple[bool, str]:
        """
        Determines whether an incoming request should be blacklisted and thus ignored. Your implementation should
        define the logic for blacklisting requests based on your needs and desired security parameters.

        Blacklist runs before the synapse data has been deserialized (i.e. before synapse.data is available).
        The synapse is instead contructed via the headers of the request. It is important to blacklist
        requests before they are deserialized to avoid wasting resources on requests that will be ignored.

        Args:
            synapse (template.protocol.Train): A synapse object constructed from the headers of the incoming request.

        Returns:
            Tuple[bool, str]: A tuple containing a boolean indicating whether the synapse's hotkey is blacklisted,
                            and a string providing the reason for the decision.

        This function is a security measure to prevent resource wastage on undesired requests. It should be enhanced
        to include checks against the metagraph for entity registration, validator status, and sufficient stake
        before deserialization of synapse data to minimize processing overhead.

        Example blacklist logic:
        - Reject if the hotkey is not a registered entity within the metagraph.
        - Consider blacklisting entities that are not validators or have insufficient stake.

        In practice it would be wise to blacklist requests from entities that are not validators, or do not have
        enough stake. This can be checked via metagraph.S and metagraph.validator_permit. You can always attain
        the uid of the sender via a metagraph.hotkeys.index( synapse.dendrite.hotkey ) call.

        Otherwise, allow the request to be processed further.
        """
        hotkey = synapse.dendrite.hotkey
        synapse_type = type(synapse).__name__

        uid = None
        axon = None
        for _uid, _axon in enumerate(self.metagraph.axons):
            if _axon.hotkey == hotkey:
                uid = _uid
                axon = _axon
                break

        if uid is None:
            bt.logging.trace(
                f"Blacklisting unrecognized hotkey: {synapse.dendrite.hotkey}"
            )
            return (
                True,
                f"Blacklisted a non registered hotkey's {synapse_type} request from {hotkey}",
            )

        if self.config.blacklist.force_validator_permit and (
            not self.config.blacklist.allow_non_registered
        ):
            # Check stake if uid is recognize
            tao = self.metagraph.neurons[uid].stake.tao
            if tao < self.config.neuron.vpermit_tao_limit:
                return (
                    True,
                    f"Blacklisted a low stake {synapse_type} request: {tao} < {self.config.neuron.vpermit_tao_limit} from {hotkey}",
                )

        if synapse.dendrite.hotkey not in self.metagraph.hotkeys:
            # Ignore requests from unrecognized entities.
            bt.logging.trace(
                f"Blacklisting unrecognized hotkey {synapse.dendrite.hotkey}"
            )
            return True, "Unrecognized hotkey"

        bt.logging.trace(
            f"Not Blacklisting recognized hotkey {synapse.dendrite.hotkey}"
        )
        return False, "Hotkey recognized!"

    async def blacklist_is_alive(
        self, synapse: template.protocol.IsAlive
    ) -> typing.Tuple[bool, str]:
        blacklist = await self.blacklist_base(synapse)
        bt.logging.debug(blacklist[1])
        return blacklist
    
    async def blacklist_all_reduce(
        self, synapse: template.protocol.AllReduce
    ) -> typing.Tuple[bool, str]:
        blacklist = await self.blacklist_base(synapse)
        bt.logging.debug(blacklist[1])
        return blacklist

    async def blacklist_train(
        self, synapse: template.protocol.Train
    ) -> typing.Tuple[bool, str]:
        blacklist = await self.blacklist_base(synapse)
        bt.logging.info(blacklist[1])
        return blacklist

    async def priority_base(self, synapse: template.protocol.Train) -> float:
        """
        The priority function determines the order in which requests are handled. More valuable or higher-priority
        requests are processed before others. You should design your own priority mechanism with care.

        This implementation assigns priority to incoming requests based on the calling entity's stake in the metagraph.

        Args:
            synapse (template.protocol.Train): The synapse object that contains metadata about the incoming request.

        Returns:
            float: A priority score derived from the stake of the calling entity.

        Miners may recieve messages from multiple entities at once. This function determines which request should be
        processed first. Higher values indicate that the request should be processed first. Lower values indicate
        that the request should be processed later.

        Example priority logic:
        - A higher stake results in a higher priority value.
        """
        caller_uid = self.metagraph.hotkeys.index(
            synapse.dendrite.hotkey
        )  # Get the caller index.
        prirority = float(
            self.metagraph.S[caller_uid]
        )  # Return the stake as the priority.
        bt.logging.trace(
            f"Prioritizing {synapse.dendrite.hotkey} with value: ", prirority
        )
        return prirority


# This is the main function, which runs the miner.
if __name__ == "__main__":
    setup_logging()
    with Miner() as miner:
        while True:
            bt.logging.info("Miner running...", time.time())
            time.sleep(5)
