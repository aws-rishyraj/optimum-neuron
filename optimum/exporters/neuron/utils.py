# coding=utf-8
# Copyright 2023 The HuggingFace Inc. team. All rights reserved.
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
"""Utility functions for neuron export."""

import copy
import os
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple, Union

import torch
from packaging import version
from transformers import PretrainedConfig

from ...neuron.utils import (
    DIFFUSION_MODEL_TEXT_ENCODER_NAME,
    DIFFUSION_MODEL_UNET_NAME,
    DIFFUSION_MODEL_VAE_DECODER_NAME,
    DIFFUSION_MODEL_VAE_ENCODER_NAME,
    get_attention_scores,
)
from ...utils import (
    DIFFUSERS_MINIMUM_VERSION,
    check_if_diffusers_greater,
    is_diffusers_available,
    logging,
)
from ...utils.import_utils import _diffusers_version
from ..tasks import TasksManager


logger = logging.get_logger()


if is_diffusers_available():
    if not check_if_diffusers_greater(DIFFUSERS_MINIMUM_VERSION.base_version):
        raise ImportError(
            f"We found an older version of diffusers {_diffusers_version} but we require diffusers to be >= {DIFFUSERS_MINIMUM_VERSION}. "
            "Please update diffusers by running `pip install --upgrade diffusers`"
        )
    from diffusers.models.attention_processor import (
        Attention,
        AttnAddedKVProcessor,
        AttnAddedKVProcessor2_0,
        AttnProcessor,
        AttnProcessor2_0,
        LoRAAttnProcessor,
        LoRAAttnProcessor2_0,
    )


if TYPE_CHECKING:
    from transformers.modeling_utils import PreTrainedModel

    from .base import NeuronConfig

    if is_diffusers_available():
        from diffusers import ModelMixin, StableDiffusionPipeline

DEFAULT_MODEL_DTYPES = {
    "text_encoder": torch.float32,
    "unet": torch.float32,
    "vae_encoder": torch.float32,
    "vae_decoder": torch.float32,
}


class DiffusersPretrainedConfig(PretrainedConfig):
    # override to update `model_type`
    def to_dict(self):
        """
        Serializes this instance to a Python dictionary.

        Returns:
            :obj:`Dict[str, any]`: Dictionary of all the attributes that make up this configuration instance.
        """
        output = copy.deepcopy(self.__dict__)
        return output


def infer_stable_diffusion_shapes_from_diffusers(
    input_shapes: Dict[str, Dict[str, int]],
    model: "StableDiffusionPipeline",
):
    sequence_length = model.tokenizer.model_max_length
    unet_num_channels = model.unet.config.in_channels
    vae_encoder_num_channels = model.vae.config.in_channels
    vae_decoder_num_channels = model.vae.config.latent_channels
    vae_scale_factor = 2 ** (len(model.vae.config.block_out_channels) - 1) or 8
    height = input_shapes["unet_input_shapes"]["height"] // vae_scale_factor
    width = input_shapes["unet_input_shapes"]["width"] // vae_scale_factor

    input_shapes["text_encoder_input_shapes"].update({"sequence_length": sequence_length})
    input_shapes["unet_input_shapes"].update(
        {"sequence_length": sequence_length, "num_channels": unet_num_channels, "height": height, "width": width}
    )
    input_shapes["vae_encoder_input_shapes"].update(
        {"num_channels": vae_encoder_num_channels, "height": height, "width": width}
    )
    input_shapes["vae_decoder_input_shapes"].update(
        {"num_channels": vae_decoder_num_channels, "height": height, "width": width}
    )

    return input_shapes


def build_stable_diffusion_components_mandatory_shapes(
    batch_size: Optional[int] = None,
    sequence_length: Optional[int] = None,
    unet_num_channels: Optional[int] = None,
    vae_encoder_num_channels: Optional[int] = None,
    vae_decoder_num_channels: Optional[int] = None,
    height: Optional[int] = None,
    width: Optional[int] = None,
):
    text_encoder_input_shapes = {"batch_size": batch_size, "sequence_length": sequence_length}
    vae_encoder_input_shapes = {
        "batch_size": batch_size,
        "num_channels": vae_encoder_num_channels,
        "height": height,
        "width": width,
    }
    vae_decoder_input_shapes = {
        "batch_size": batch_size,
        "num_channels": vae_decoder_num_channels,
        "height": height,
        "width": width,
    }
    unet_input_shapes = {
        "batch_size": batch_size,
        "sequence_length": sequence_length,
        "num_channels": unet_num_channels,
        "height": height,
        "width": width,
    }

    components_shapes = {
        "text_encoder_input_shapes": text_encoder_input_shapes,
        "unet_input_shapes": unet_input_shapes,
        "vae_encoder_input_shapes": vae_encoder_input_shapes,
        "vae_decoder_input_shapes": vae_decoder_input_shapes,
    }

    return components_shapes


def get_stable_diffusion_models_for_export(
    pipeline: "StableDiffusionPipeline",
    model_kwargs: Dict[str, Any],
    text_encoder_input_shapes: Dict[str, int],
    unet_input_shapes: Dict[str, int],
    vae_encoder_input_shapes: Dict[str, int],
    vae_decoder_input_shapes: Dict[str, int],
    dynamic_batch_size: Optional[bool] = False,
    model_dtypes: Dict[str, torch.dtype] = DEFAULT_MODEL_DTYPES,
) -> Dict[str, Tuple[Union["PreTrainedModel", "ModelMixin"], "NeuronConfig"]]:
    """
    Returns the components of a Stable Diffusion model and their subsequent neuron configs.
    These components are chosen because they represent the bulk of the compute in the pipeline,
    and performance benchmarking has shown that running them on Neuron yields significant
    performance benefit (CLIP text encoder, VAE encoder, VAE decoder, Unet).

    Args:
        pipeline ([`StableDiffusionPipeline`]):
            Stable diffusion pipeline with float32 as dtype.
        model_kwargs (Dict[str, Any]):
            Keyword arguments to pass to the model `.from_pretrained()` method.
        text_encoder_input_shapes (`Dict[str, int]`):
            Static shapes used for compiling text encoder.
        unet_input_shapes (`Dict[str, int]`):
            Static shapes used for compiling unet.
        vae_encoder_input_shapes (`Dict[str, int]`):
            Static shapes used for compiling vae encoder.
        vae_decoder_input_shapes (`Dict[str, int]`):
            Static shapes used for compiling vae decoder.
        dynamic_batch_size (`bool`, defaults to `False`):
            Whether the Neuron compiled model supports dynamic batch size.
        model_dtypes (`Dict[str, torch.dtype]`, defaults to `DEFAULT_MODEL_DTYPES`.)
            Data type of stable diffusion models.

    Returns:
        `Dict[str, Tuple[Union[`PreTrainedModel`, `ModelMixin`], `NeuronConfig`]: A Dict containing the model and
        Neuron configs for the different components of the model.
    """
    models_for_export = _get_submodels_for_export_stable_diffusion(pipeline, model_kwargs, model_dtypes)

    # Text encoder
    text_encoder = models_for_export[DIFFUSION_MODEL_TEXT_ENCODER_NAME]
    text_encoder_config_constructor = TasksManager.get_exporter_config_constructor(
        model=text_encoder, exporter="neuron", task="feature-extraction"
    )
    text_encoder_neuron_config = text_encoder_config_constructor(
        text_encoder.config,
        task="feature-extraction",
        dynamic_batch_size=dynamic_batch_size,
        **text_encoder_input_shapes,
    )
    models_for_export[DIFFUSION_MODEL_TEXT_ENCODER_NAME] = (text_encoder, text_encoder_neuron_config)

    # U-NET
    unet = models_for_export[DIFFUSION_MODEL_UNET_NAME]
    unet_neuron_config_constructor = TasksManager.get_exporter_config_constructor(
        model=unet, exporter="neuron", task="semantic-segmentation", model_type="unet"
    )
    unet_neuron_config = unet_neuron_config_constructor(
        unet.config,
        task="semantic-segmentation",
        dynamic_batch_size=dynamic_batch_size,
        **unet_input_shapes,
    )
    models_for_export[DIFFUSION_MODEL_UNET_NAME] = (unet, unet_neuron_config)

    # VAE Encoder
    vae_encoder = models_for_export[DIFFUSION_MODEL_VAE_ENCODER_NAME]
    vae_encoder_config_constructor = TasksManager.get_exporter_config_constructor(
        model=vae_encoder,
        exporter="neuron",
        task="semantic-segmentation",
        model_type="vae-encoder",
    )
    vae_encoder_neuron_config = vae_encoder_config_constructor(
        vae_encoder.config,
        task="semantic-segmentation",
        dynamic_batch_size=dynamic_batch_size,
        **vae_encoder_input_shapes,
    )
    models_for_export[DIFFUSION_MODEL_VAE_ENCODER_NAME] = (vae_encoder, vae_encoder_neuron_config)

    # VAE Decoder
    vae_decoder = models_for_export[DIFFUSION_MODEL_VAE_DECODER_NAME]
    vae_decoder_config_constructor = TasksManager.get_exporter_config_constructor(
        model=vae_decoder,
        exporter="neuron",
        task="semantic-segmentation",
        model_type="vae-decoder",
    )
    vae_decoder_neuron_config = vae_decoder_config_constructor(
        vae_decoder.config,
        task="semantic-segmentation",
        dynamic_batch_size=dynamic_batch_size,
        **vae_decoder_input_shapes,
    )
    models_for_export[DIFFUSION_MODEL_VAE_DECODER_NAME] = (vae_decoder, vae_decoder_neuron_config)

    return models_for_export


def _get_submodels_for_export_stable_diffusion(
    pipeline: "StableDiffusionPipeline",
    model_kwargs: Dict[str, Any],
    model_dtypes: Dict[str, torch.dtype] = DEFAULT_MODEL_DTYPES,
) -> Dict[str, Union["PreTrainedModel", "ModelMixin"]]:
    """
    Args:
        model_kwargs (Dict[str, Any]):
            Keyword arguments to pass to the model `.from_pretrained()` method.
        model_dtypes (`Dict[str, torch.dtype]`):
            Mapping of stable diffusion models and their data type. For example: {"text_encoder": torch.float32,
            "unet": torch.bfloat16, "vae_encoder": torch.float32, "vae_decoder": torch.float32}

    Returns the components of a Stable Diffusion model.
    """
    models_for_export = {}
    model_dtype_groups = {
        dtype: [model_name for model_name in model_dtypes.keys() if model_dtypes[model_name] == dtype]
        for dtype in set(model_dtypes.values())
    }

    if torch.float32 in model_dtype_groups.keys():
        # Put models on float on the top to reuse the pipeline
        model_dtype_groups = {torch.float32: model_dtype_groups.pop(torch.float32), **model_dtype_groups}
    else:
        del pipeline
        pipeline = None

    model_init_funcs = {
        DIFFUSION_MODEL_TEXT_ENCODER_NAME: _get_text_encoder_for_export,
        DIFFUSION_MODEL_UNET_NAME: _get_unet_for_export,
        DIFFUSION_MODEL_VAE_ENCODER_NAME: _get_vae_encoder_for_export,
        DIFFUSION_MODEL_VAE_DECODER_NAME: _get_vae_decoder_for_export,
    }

    for model_dtype, model_names in model_dtype_groups.items():
        if pipeline is None:
            pipeline = TasksManager.get_model_from_task(**model_kwargs, torch_dtype=model_dtype)
        for model_name in model_names:
            models_for_export[model_name] = model_init_funcs[model_name](pipeline)
        del pipeline

    return models_for_export


def _get_text_encoder_for_export(pipeline):
    if pipeline.text_encoder is not None:
        return copy.deepcopy(pipeline.text_encoder)


def _get_unet_for_export(pipeline):
    projection_dim = pipeline.text_encoder.config.projection_dim
    pipeline.unet.set_attn_processor(AttnProcessor())
    pipeline.unet.config.text_encoder_projection_dim = projection_dim
    # The U-NET time_ids inputs shapes depends on the value of `requires_aesthetics_score`
    # https://github.com/huggingface/diffusers/blob/v0.18.2/src/diffusers/pipelines/stable_diffusion_xl/pipeline_stable_diffusion_xl_img2img.py#L571
    pipeline.unet.config.requires_aesthetics_score = getattr(pipeline.config, "requires_aesthetics_score", False)

    # Replace original cross-attention module with custom cross-attention module for better performance
    # For applying optimized attention score, we need to set env variable  `NEURON_FUSE_SOFTMAX=1`
    if os.environ.get("NEURON_FUSE_SOFTMAX") == "1":
        Attention.get_attention_scores = get_attention_scores
    else:
        logger.warning(
            "You are not applying optimized attention score computation. If you want better performance, please"
            " set the environment variable with `export NEURON_FUSE_SOFTMAX=1` and recompile the unet model."
        )
    return copy.deepcopy(pipeline.unet)


def _get_vae_encoder_for_export(pipeline):
    vae_encoder = copy.deepcopy(pipeline.vae)
    if not version.parse(torch.__version__) >= version.parse("2.1.0"):
        vae_encoder = override_diffusers_2_0_attn_processors(vae_encoder)
    vae_encoder.forward = lambda sample: {"latent_sample": vae_encoder.encode(x=sample)["latent_dist"].sample()}
    return vae_encoder


def _get_vae_decoder_for_export(pipeline):
    vae_decoder = copy.deepcopy(pipeline.vae)
    if not version.parse(torch.__version__) >= version.parse("2.1.0"):
        vae_decoder = override_diffusers_2_0_attn_processors(vae_decoder)
    vae_decoder.forward = lambda latent_sample: vae_decoder.decode(z=latent_sample)
    return vae_decoder


def override_diffusers_2_0_attn_processors(model):
    for _, submodule in model.named_modules():
        if isinstance(submodule, Attention):
            if isinstance(submodule.processor, AttnProcessor2_0):
                submodule.set_processor(AttnProcessor())
            elif isinstance(submodule.processor, LoRAAttnProcessor2_0):
                lora_attn_processor = LoRAAttnProcessor(
                    hidden_size=submodule.processor.hidden_size,
                    cross_attention_dim=submodule.processor.cross_attention_dim,
                    rank=submodule.processor.rank,
                    network_alpha=submodule.processor.to_q_lora.network_alpha,
                )
                lora_attn_processor.to_q_lora = copy.deepcopy(submodule.processor.to_q_lora)
                lora_attn_processor.to_k_lora = copy.deepcopy(submodule.processor.to_k_lora)
                lora_attn_processor.to_v_lora = copy.deepcopy(submodule.processor.to_v_lora)
                lora_attn_processor.to_out_lora = copy.deepcopy(submodule.processor.to_out_lora)
                submodule.set_processor(lora_attn_processor)
            elif isinstance(submodule.processor, AttnAddedKVProcessor2_0):
                submodule.set_processor(AttnAddedKVProcessor())
    return model
