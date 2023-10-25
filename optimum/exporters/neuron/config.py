# coding=utf-8
# Copyright 2023 The HuggingFace Team. All rights reserved.
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
"""
Common Neuron configuration classes that handle most of the features for building model specific
configurations.
"""
from typing import Dict, List

from ...utils import (
    DummyBboxInputGenerator,
    DummyInputGenerator,
    DummySeq2SeqDecoderTextInputGenerator,
    DummySeq2SeqPastKeyValuesGenerator,
    DummyTextInputGenerator,
    DummyVisionInputGenerator,
    logging,
)
from .base import NeuronConfig, NeuronDecoderConfig


logger = logging.get_logger(__name__)


class TextEncoderNeuronConfig(NeuronConfig):
    """
    Handles encoder-based text architectures.
    """

    DUMMY_INPUT_GENERATOR_CLASSES = (DummyTextInputGenerator,)
    MANDATORY_AXES = ("batch_size", "sequence_length", ("multiple-choice", "num_choices"))


class VisionNeuronConfig(NeuronConfig):
    """
    Handles vision architectures.
    """

    DUMMY_INPUT_GENERATOR_CLASSES = (DummyVisionInputGenerator,)
    MANDATORY_AXES = ("batch_size", "num_channels", "width", "height")


class TextAndVisionNeuronConfig(NeuronConfig):
    """
    Handles multi-modal text and vision architectures.
    """

    DUMMY_INPUT_GENERATOR_CLASSES = (DummyTextInputGenerator, DummyVisionInputGenerator, DummyBboxInputGenerator)


class TextNeuronDecoderConfig(NeuronDecoderConfig):
    """
    Handles text decoder architectures.
    """

    pass


class TextSeq2SeqNeuronConfig(NeuronConfig):
    """
    Handles encoder-decoder-based text architectures.
    """

    DUMMY_INPUT_GENERATOR_CLASSES = (
        DummyTextInputGenerator,
        DummySeq2SeqDecoderTextInputGenerator,
        DummySeq2SeqPastKeyValuesGenerator,
    )

    @property
    def is_decoder(self) -> bool:
        raise NotImplementedError()

    @property
    def inputs(self) -> Dict[str, Dict[int, str]]:
        common_inputs = []
        # encoder + decoder without past
        if "encoder" in self.MODEL_TYPE:
            common_inputs = ["input_ids", "attention_mask"]

        # decoder with past
        if "decoder" in self.MODEL_TYPE:
            common_inputs = [
                "decoder_input_ids",
                "decoder_attention_mask",
                "encoder_hidden_states",
                "attention_mask",  # TODO: replace with `encoder_attention_mask` after optimum 1.14 release
            ]

        return common_inputs

    @property
    def outputs(self) -> Dict[str, Dict[int, str]]:
        # encoder + decoder without past
        if "encoder" in self.MODEL_TYPE:
            common_outputs = ["present_key_values_self_attn", "past_key_values_cross_attn"]
        # decoder with past
        if "decoder" in self.MODEL_TYPE:
            common_outputs = ["next_tokens", "past_key_values_self_attn", "past_key_values_cross_attn"]
        return common_outputs

    def _create_dummy_input_generator_classes(self, **kwargs) -> List["DummyInputGenerator"]:
        dummy_text_input_generator = self.DUMMY_INPUT_GENERATOR_CLASSES[0](
            self.task, self._normalized_config, **kwargs
        )
        dummy_decoder_text_input_generator = self.DUMMY_INPUT_GENERATOR_CLASSES[1](
            self.task,
            self._normalized_config,
            **kwargs,
        )
        dummy_seq2seq_past_key_values_generator = self.DUMMY_INPUT_GENERATOR_CLASSES[2](
            self.task,
            self._normalized_config,
            encoder_sequence_length=dummy_text_input_generator.sequence_length,
            **kwargs,
        )
        dummy_inputs_generators = [
            dummy_text_input_generator,
            dummy_decoder_text_input_generator,
            dummy_seq2seq_past_key_values_generator,
        ]

        return dummy_inputs_generators
