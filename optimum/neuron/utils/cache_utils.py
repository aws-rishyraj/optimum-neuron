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
"""Utilities for caching."""

import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union

import torch
from huggingface_hub import HfApi, HfFolder, snapshot_download

from ...utils import logging
from .version_utils import get_neuron_compiler_version


if TYPE_CHECKING:
    from transformers import PreTrainedModel


logger = logging.get_logger()


HF_API = HfApi()
HF_FOLDER = HfFolder()
HF_TOKEN = HF_FOLDER.get_token()

HASH_FILE_NAME = "pytorch_model.bin"
HF_HUB_CACHE_REPOS = ["michaelbenayoun/cache_test"]


def get_neuron_cache_path() -> Optional[Path]:
    neuron_cc_flags = os.environ.get("NEURON_CC_FLAGS", "")
    if "--no-cache" in neuron_cc_flags:
        return None
    else:
        match_ = re.search(r"--cache_dir=([\w\/]+)", neuron_cc_flags)
        if match_:
            path = Path(match_.group(1))
        else:
            path = Path("/var/tmp")

        return path / "neuron-compile-path"


def compute_file_sha256_hash(filename: Union[str, Path]) -> str:
    if isinstance(filename, Path):
        filename = filename.as_posix()

    file_hash = hashlib.sha256()
    with open(filename, "rb") as f:
        fb = f.read()
        file_hash.update(fb)
    return file_hash.hexdigest()


class StaticTemporaryDirectory:
    def __init__(self, dirname: Union[str, Path]):
        if isinstance(dirname, str):
            dirname = Path(dirname)
        if dirname.exists():
            raise FileExistsError(
                f"{dirname} already exists, cannot create a static temporary directory witht this name."
            )
        self.dirname = dirname

    def __enter__(self):
        self.dirname.mkdir(parents=True)
        return self.dirname

    def __exit__(self, *exc):
        shutil.rmtree(self.dirname)


@dataclass
class NeuronHash:
    model: "PreTrainedModel"
    input_shapes: List[int]
    data_type: torch.dtype
    num_neuron_cores: int = -1
    neuron_compiler_version: str = ""

    def __post_init__(self):
        if not self.neuron_compiler_version:
            self.neuron_compiler_version = get_neuron_compiler_version()
        if self.num_neuron_cores < 0:
            self.num_neuron_cores = int(os.environ.get("LOCAL_WORLD_SIZE", "1"))

    @property
    def hash_dict(self) -> Dict[str, Any]:
        hash_dict = asdict(self)
        # hash_dict["model"] = hash_dict["model"].state_dict
        return hash_dict

    def compute_hash(self) -> Tuple[str, str]:
        model_hash = ""
        with tempfile.TemporaryDirectory() as tmpdirname:
            filename = Path(tmpdirname) / HASH_FILE_NAME
            torch.save(self.model.state_dict(), filename)
            model_hash = compute_file_sha256_hash(filename)

        overall_hash = ""
        hash_dict = self.hash_dict
        with tempfile.TemporaryDirectory() as tmpdirname:
            filename = Path(tmpdirname) / HASH_FILE_NAME
            torch.save(hash_dict, filename)
            overall_hash = compute_file_sha256_hash(filename)
        return model_hash, overall_hash

    @property
    def folders(self) -> List[str]:
        model_hash, overall_hash = self.compute_hash()
        return [
            self.neuron_compiler_version,
            self.model.config.model_type,
            model_hash,
            overall_hash,
        ]

    @property
    def cache_path(self) -> Path:
        return Path("/".join(self.folders))


@dataclass
class CachedModelOnTheHub:
    repo_id: str
    folder: Union[str, Path]
    revision: str = "main"

    def __post_init__(self):
        if isinstance(self.folder, Path):
            self.folder = self.folder.as_posix()


def get_cached_model_on_the_hub(neuron_hash: NeuronHash) -> Optional[CachedModelOnTheHub]:
    target_directory = neuron_hash.cache_path

    cache_repo_id = None
    cache_revision = None

    for repo_id in HF_HUB_CACHE_REPOS:
        if isinstance(repo_id, tuple):
            repo_id, revision = repo_id
        else:
            revision = "main"
        repo_filenames = map(Path, HfApi().list_repo_files(repo_id, revision=revision, token=HF_TOKEN))
        for repo_filename in repo_filenames:
            if repo_filename.parent == target_directory:
                cache_repo_id = repo_id
                cache_revision = revision
                break

    if cache_repo_id is None:
        cached_model = None
    else:
        cached_model = CachedModelOnTheHub(cache_repo_id, target_directory, revision=cache_revision)

    return cached_model


def download_cached_model_from_hub(
    neuron_hash: NeuronHash, target_directory: Optional[Union[str, Path]] = None
) -> bool:
    if target_directory is None:
        target_directory = get_neuron_cache_path()

    cached_model = get_cached_model_on_the_hub(neuron_hash)
    if cached_model is not None:
        # TODO: validate if the tree structure is preserved or not?
        snapshot_download(
            repo_id=cached_model.repo_id,
            revision=cached_model.revision,
            repo_type="model",
            local_dir=target_directory,
            local_dir_use_symlinks=False,
            allow_patterns=f"{cached_model.folder}/**",
        )
    return cached_model is not None


def push_to_cache_on_hub(
    neuron_hash: NeuronHash,
    local_cache_directory: str,
    cache_repo_id: Optional[str] = None,
    overwrite_existing: bool = False,
) -> CachedModelOnTheHub:
    target_directory = neuron_hash.cache_path
    if cache_repo_id is None:
        cache_repo_id = HF_HUB_CACHE_REPOS[0]

    if not overwrite_existing:
        repo_filenames = map(Path, HfApi().list_repo_files(cache_repo_id, token=HF_TOKEN))
        exists = any(filename.parent == target_directory for filename in repo_filenames)
        if exists:
            logger.info(
                f"Did push the cached model located at {local_cache_directory} to the repo named {cache_repo_id} "
                "because it already exists there. Use overwrite_existing=True if you want to overwrite the cache on the "
                "Hub."
            )

    HF_API.upload_folder(
        folder_path=local_cache_directory,
        path_in_repo=target_directory.as_posix(),
        repo_id=cache_repo_id,
        repo_type="model",
    )
    return CachedModelOnTheHub(cache_repo_id, target_directory)
