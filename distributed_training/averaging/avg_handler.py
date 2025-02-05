import asyncio
from typing import Any, Dict, List, Tuple

import bittensor as bt
import numpy as np
import torch

import distributed_training
from distributed_training.protocol import AllReduce


class AllReduceError(Exception):
    """Base exception for AllReduce-related errors."""

    pass


class GradientAveragingTimeoutError(AllReduceError):
    """Raised when gradient averaging step times out."""

    pass


class GradientAveragingError(AllReduceError):
    """Raised when gradient averaging fails for non-timeout reasons."""

    pass


class StateAveragingError(AllReduceError):
    """Raised when state averaging fails."""

    pass


# TODO cleanup code after moving to diloco
class AveragingHandler:
    """Handles averaging round and outer step for both validators and miners."""

    def __init__(
        self,
        model,
        grad_averager,
        state_averager,
        model_loading_manager=None,
    ):
        self.model = model
        self.grad_averager = grad_averager
        self.state_averager = state_averager
        self.model_loading_manager = model_loading_manager

    def _get_weights_sample(self) -> List[float]:
        """Get a sample of model weights for validation."""
        return [layer for layer in self.model.parameters()][-2][-10:].tolist()

    async def _validate_weight_update(self, initial_weights: List[float]) -> bool:
        """Validate model weight updates."""
        final_weights = self._get_weights_sample()

        if final_weights == initial_weights:
            raise ModelStateError("Weights unchanged after update")

        if sum(np.isnan(final_weights)) > 1:
            raise ModelStateError("NaN values detected in weights after update")

        return True

    async def run_validator_allreduce(
        self, timeout: int, dendrite_pool, miner_uids, #bandwidth
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Process allreduce specifically for validator.

        Returns:
            Tuple[bool, Dict[str, Any]]: (success, results)
            - success: True if allreduce completed successfully, False otherwise
            - results: Dictionary containing peers and bandwidth info if successful, empty dict if failed
        """
        grad_averager_step = None
        query_tasks = []

        try:
            # Clip gradients
            torch.nn.utils.clip_grad_norm_(self.model.parameters(), 1.0)

            # Used for load balancing and scoring
            # self.grad_averager.bandwidth = bandwidth["download"]

            bt.logging.info("Starting Pseudo Gradient Averaging..")
            # Start gradient averaging without waiting
            grad_averager_step = self.grad_averager.step(
                wait=False,
                timeout=timeout,
            )

            # Send AllReduce query to pause miner training and perform global sync
            query_tasks.append(
                dendrite_pool.async_forward(
                    miner_uids,
                    [AllReduce() for _ in miner_uids],
                    timeout=timeout,
                )
            )
            bt.logging.info(
                ":wait: AllReduce Query Sent Out. Waiting for AllReduce to finish.."
            )

            # First wait for queries to complete
            await asyncio.gather(*query_tasks)

            averaging_result = await grad_averager_step
         
            if averaging_result is None:
                bt.logging.error("Averaging Failed")
                return False, {}

            failed_peers, participating_peers, modes, bandwidths = averaging_result

            self.grad_averager.notify_used_averaged_gradients()
            bt.logging.success("Finished Averaging Pseudo Gradients!")

            initial_weights = self._get_weights_sample()
            bt.logging.debug(f"Initial weights sample: {initial_weights}")

            # Perform offloaded outer optimization steps
            bt.logging.info("Performing Outer Optimizer Step..")
            self.state_averager.step(
                increment_epoch=True, optimizer_step=True, zero_grad=False
            )
            self.state_averager.update_main_param_after_outer_step()
            self.state_averager.optimizer.zero_grad()
            bt.logging.success(
                ":white_heavy_check_mark: Finished Outer Optimizer Step!"
            )

            # Validate weight updates
            await self._validate_weight_update(initial_weights)

            return True, {
                "failed_peers": failed_peers,
                "participating_peers": participating_peers,
                "modes": modes,
                "bandwidths": bandwidths,
            }

        except Exception as e:
            bt.logging.error(f"Error during AllReduce setup: {str(e)}")
            return False, {}

        finally:
            if grad_averager_step:
                grad_averager_step.cancel()

    def calculate_allreduce_scores(
        self,
        participating_peers: list,
        failed_peers: list,
        peerids_to_uids: dict,
        modes: list = None,
        bandwidths: list = None,
    ) -> dict:
        """
        Calculate scores based on AllReduce participation status, modes, and bandwidths.

        Args:
            participating_peers (list): List of peers that participated in AllReduce
            failed_peers (list): List of peers that failed during AllReduce
            peerids_to_uids (dict): Mapping of peer IDs to UIDs
            modes (list, optional): List of modes for each participating peer
            bandwidths (list, optional): List of bandwidths for each participating peer

        Returns:
            dict: Scores for each UID based on participation and optional mode/bandwidth
        """
        # Convert peer IDs to UIDs
        participating_uids = []
        uid_modes = {}
        uid_bandwidths = {}

        for idx, peer in enumerate(participating_peers):
            uid = peerids_to_uids.get(str(peer), "'''")
            participating_uids.append(uid)
            if modes is not None:
                uid_modes[uid] = modes[idx]
            if bandwidths is not None:
                uid_bandwidths[uid] = bandwidths[idx]

        failed_uids = [
            peerids_to_uids.get(str(failed_peer), "'''") for failed_peer in failed_peers
        ]

        # Calculate participation metrics
        successful_peers_count = len(participating_peers) - len(failed_peers)

        # Update event metrics
        self.event.update(
            {
                "failed_peers_count": len(failed_peers),
                "participating_peers_count": len(participating_peers),
                "successful_peers_count": successful_peers_count,
            }
        )

        # Find max bandwidth for normalization if bandwidths are provided
        max_bandwidth = max(bandwidths) if bandwidths else 1.0

        # Initialize scores dictionary
        scores = {}
        status_dict = {}

        for uid in range(256):  # Assuming 256 UIDs in metagraph
            str_uid = str(uid)
            if uid in participating_uids and uid not in failed_uids:
                # Base score for successful participation
                base_score = 1.0
                final_score = base_score
                status = "SUCCESS"

                # Apply mode penalty if modes are provided
                if modes is not None and uid in uid_modes:
                    if uid_modes[uid] == "AveragingMode.CLIENT":
                        final_score = 0.0
                        status = "WRONG_MODE"

                # Apply bandwidth bonus if bandwidths are provided
                if (
                    bandwidths is not None
                    and uid in uid_bandwidths
                    and status != "WRONG_MODE"
                ):
                    bandwidth_bonus = 0.5 * (uid_bandwidths[uid] / max_bandwidth)
                    final_score += bandwidth_bonus
                    bt.logging.debug(
                        f"UID {uid} score breakdown - Base: {base_score:.2f}, Bandwidth bonus: {bandwidth_bonus:.2f}"
                    )

                scores[str_uid] = final_score
                status_dict[str_uid] = status

            elif uid in failed_uids:
                scores[str_uid] = 0.0
                status_dict[str_uid] = "FAIL"
            else:
                scores[str_uid] = 0.0
                status_dict[str_uid] = "NON_PARTICIPATING"

        # Log participation and scoring details
        bt.logging.info(f"Failed UIDs: {failed_uids}")
        bt.logging.info(f"Participating UIDs: {participating_uids}")
        if modes is not None:
            bt.logging.debug(f"Modes by UID: {uid_modes}")
        if bandwidths is not None:
            bt.logging.debug(f"Bandwidths by UID: {uid_bandwidths}")
        bt.logging.info(f"AllReduce UID Scores: {scores}")

        # Store status in model config
        self.all_reduce_scores = status_dict

        return scores

    @staticmethod
    async def _wait_for_model_loading(model_loading_manager):
        """Wait for any ongoing model loading to complete."""
        if model_loading_manager:
            while model_loading_manager.is_loading:
                await asyncio.sleep(1)

    async def run_miner_allreduce(
        self, synapse
    ) -> distributed_training.protocol.AllReduce:
        """Process allreduce specifically for miner."""
        await self._wait_for_model_loading(self.model_loading_manager)

        if self.model_loading_manager:
            self.model_loading_manager.set_loading_state(True)
        # TODO Weight/gradient validation
        grad_averager_step = None
        try:
            # Clip gradients
            torch.nn.utils.clip_grad_norm_(self.model.parameters(), 1.0)

            # # Used for load balancing and scoring
            # self.grad_averager.bandwidth = bandwidth[
            #     "download"
            # ]  # TODO Either use average bandwidth or set each time here

            try:
                bt.logging.info(":wait: Starting Pseudo Gradient Averaging..")
                grad_averager_step = self.grad_averager.step(
                    wait=True, timeout=synapse.timeout
                )
            except asyncio.TimeoutError:
                raise GradientAveragingTimeoutError("Gradient averaging step timed out")
            except Exception as e:
                raise GradientAveragingError(f"Gradient averaging failed: {str(e)}")

            self.grad_averager.notify_used_averaged_gradients()
            bt.logging.success("Finished Averaging Pseudo Gradients!")

            initial_weights = self._get_weights_sample()
            bt.logging.debug(f"Initial weights sample: {initial_weights}")

            # Perform offloaded outer optimization steps
            bt.logging.info("Performing Outer Optimizer Step..")
            self.state_averager.step(
                increment_epoch=True, optimizer_step=True, zero_grad=False
            )
            self.state_averager.update_main_param_after_outer_step()
            self.state_averager.optimizer.zero_grad()
            bt.logging.success("Finished Outer Optimizer Step!")
            bt.logging.success(
                ":white_heavy_check_mark: Finished Outer Optimizer Step!"
            )

            # Validate weight updates
            await self._validate_weight_update(initial_weights)

            synapse.completion = "True"
            return synapse

        except Exception as e:
            raise AllReduceError(f"Unexpected error during AllReduce: {str(e)}") from e

        finally:
            if grad_averager_step:
                grad_averager_step.cancel()
            if self.model_loading_manager:
                self.model_loading_manager.set_loading_state(False)
