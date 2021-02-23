# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
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

import itertools
from nemo.collections.nlp.modules.common.tokenizer_utils import get_nmt_tokenizer
from nemo.collections.nlp.modules.common.lm_utils import get_transformer
import pickle
import random
from pathlib import Path
from typing import Dict, List, Optional

import jieba
import numpy as np
import opencc
import torch
import torch.utils.data as pt_data
from omegaconf import DictConfig, OmegaConf
from pytorch_lightning import Trainer
from pytorch_lightning.core.lightning import LightningModule
from pytorch_lightning.overrides.data_parallel import LightningDistributedDataParallel
from pytorch_lightning.utilities import rank_zero_only
from sacrebleu import corpus_bleu
from sacremoses import MosesDetokenizer, MosesPunctNormalizer, MosesTokenizer
from torch.nn.parallel.distributed import DistributedDataParallel

from nemo.collections.common.losses import SmoothedCrossEntropyLoss
from nemo.collections.common.metrics import GlobalAverageLossMetric
from nemo.collections.common.parts import transformer_weights_init
from nemo.collections.common.tokenizers.pangu_jieba_detokenizer import PanguJiebaDetokenizer
from nemo.collections.common.tokenizers.sentencepiece_detokenizer import SentencePieceDetokenizer
from nemo.collections.nlp.data import TarredTranslationDataset, TranslationDataset
from nemo.collections.nlp.models.enc_dec_nlp_model import EncDecNLPModel
from nemo.collections.nlp.models.machine_translation.mt_enc_dec_config import MTEncDecModelConfig
from nemo.collections.nlp.modules.common import TokenClassifier
from nemo.collections.nlp.modules.common.transformer import BeamSearchSequenceGenerator, TopKSequenceGenerator
from nemo.collections.nlp.modules.common.transformer.transformer import TransformerDecoderNM, TransformerEncoderNM
from nemo.core.classes.common import typecheck
from nemo.utils import logging, model_utils

__all__ = ['MTEncDecModel']


class MTEncDecModel(EncDecNLPModel):
    """
    Encoder-decoder machine translation model.
    """

    def __init__(self, cfg: MTEncDecModelConfig, trainer: Trainer = None):
        cfg = model_utils.convert_model_config_to_dict_config(cfg)
        # Get global rank and total number of GPU workers for IterableDataset partitioning, if applicable
        self.global_rank = 0
        self.world_size = 1
        if trainer is not None:
            self.global_rank = (trainer.node_rank * trainer.num_gpus) + trainer.local_rank
            self.world_size = trainer.num_nodes * trainer.num_gpus

        cfg = model_utils.maybe_update_config_version(cfg)

        # Instaniate tokenizers and register to be saved with NeMo Model archive
        self.setup_enc_dec_tokenizers(
            encoder_tokenizer_library=cfg.encoder_tokenizer.get('tokenizer_library', 'yttm'),
            encoder_tokenizer_model=cfg.encoder_tokenizer.get('tokenizer_model'),
            encoder_bpe_dropout=cfg.encoder_tokenizer.get('bpe_dropout', 0.0),
            encoder_model_name=cfg.encoder.get('model_name'),
            decoder_tokenizer_library=cfg.decoder_tokenizer.get('tokenizer_library', 'yttm'),
            decoder_tokenizer_model=cfg.decoder_tokenizer.tokenizer_model,
            decoder_bpe_dropout=cfg.decoder_tokenizer.get('bpe_dropout', 0.0),
            decoder_model_name=cfg.decoder.get('model_name'),
        )

        self.src_language: str = cfg.get("src_language", None)
        self.tgt_language: str = cfg.get("tgt_language", None)

        super().__init__(cfg=cfg, trainer=trainer)

        # encoder from NeMo, Megatron-LM, or HuggingFace
        encoder_cfg_dict = OmegaConf.to_container(cfg.get('encoder'))
        encoder_cfg_dict['vocab_size'] = self.encoder_vocab_size
        library = encoder_cfg_dict.pop('library', 'nemo')
        model_name = encoder_cfg_dict.pop('model_name', None)
        pretrained = encoder_cfg_dict.pop('pretrained', False)
        self.encoder = get_transformer(
            library=library, model_name=model_name, pretrained=pretrained, config_dict=encoder_cfg_dict, encoder=True
        )

        # decoder from NeMo, Megatron-LM, or HuggingFace
        decoder_cfg_dict = OmegaConf.to_container(cfg.get('decoder'))
        decoder_cfg_dict['vocab_size'] = self.decoder_vocab_size
        library = decoder_cfg_dict.pop('library', 'nemo')
        model_name = decoder_cfg_dict.pop('model_name', None)
        pretrained = decoder_cfg_dict.pop('pretrained', False)
        self.decoder = get_transformer(
            library=library, model_name=model_name, pretrained=pretrained, config_dict=encoder_cfg_dict, encoder=False
        )

        self.log_softmax = TokenClassifier(
            hidden_size=self.decoder.hidden_size,
            num_classes=self.decoder_vocab_size,
            activation=cfg.head.activation,
            log_softmax=cfg.head.log_softmax,
            dropout=cfg.head.dropout,
            use_transformer_init=cfg.head.use_transformer_init,
        )

        self.beam_search = BeamSearchSequenceGenerator(
            embedding=self.decoder.embedding,
            decoder=self.decoder.decoder,
            log_softmax=self.log_softmax,
            max_sequence_length=self.decoder.max_sequence_length,
            beam_size=cfg.beam_size,
            bos=self.decoder_tokenizer.bos_id,
            pad=self.decoder_tokenizer.pad_id,
            eos=self.decoder_tokenizer.eos_id,
            len_pen=cfg.len_pen,
            max_delta_length=cfg.max_generation_delta,
        )

        # tie weights of embedding and softmax matrices
        self.log_softmax.mlp.layer0.weight = self.decoder.embedding.token_embedding.weight

        # TODO: encoder and decoder with different hidden size?
        std_init_range = 1 / self.encoder.hidden_size ** 0.5
        self.apply(lambda module: transformer_weights_init(module, std_init_range))

        self.loss_fn = SmoothedCrossEntropyLoss(
            pad_id=self.decoder_tokenizer.pad_id, label_smoothing=cfg.label_smoothing
        )
        self.eval_loss = GlobalAverageLossMetric(dist_sync_on_step=False, take_avg_loss=True)

    def filter_predicted_ids(self, ids):
        ids[ids >= self.decoder_tokenizer.vocab_size] = self.decoder_tokenizer.unk_id
        return ids

    @typecheck()
    def forward(self, src, src_mask, tgt, tgt_mask):
        src_hiddens = self.encoder(src, src_mask)
        tgt_hiddens = self.decoder(tgt, tgt_mask, src_hiddens, src_mask)
        log_probs = self.log_softmax(hidden_states=tgt_hiddens)
        return log_probs

    def training_step(self, batch, batch_idx):
        """
        Lightning calls this inside the training loop with the data from the training dataloader
        passed in as `batch`.
        """
        # forward pass
        for i in range(len(batch)):
            if batch[i].ndim == 3:
                # Dataset returns already batched data and the first dimension of size 1 added by DataLoader
                # is excess.
                batch[i] = batch[i].squeeze(dim=0)
        src_ids, src_mask, tgt_ids, tgt_mask, labels = batch
        log_probs = self(src_ids, src_mask, tgt_ids, tgt_mask)
        train_loss = self.loss_fn(log_probs=log_probs, labels=labels)
        tensorboard_logs = {
            'train_loss': train_loss,
            'lr': self._optimizer.param_groups[0]['lr'],
        }
        return {'loss': train_loss, 'log': tensorboard_logs}

    def eval_step(self, batch, batch_idx, mode):
        for i in range(len(batch)):
            if batch[i].ndim == 3:
                # Dataset returns already batched data and the first dimension of size 1 added by DataLoader
                # is excess.
                batch[i] = batch[i].squeeze(dim=0)
        src_ids, src_mask, tgt_ids, tgt_mask, labels = batch
        log_probs = self(src_ids, src_mask, tgt_ids, tgt_mask)

        src_hiddens = self.encoder(src_ids, src_mask)

        beam_results = self.beam_search(encoder_hidden_states=src_hiddens, encoder_input_mask=src_mask)
        beam_results = self.filter_predicted_ids(beam_results)

        eval_loss = self.loss_fn(log_probs=log_probs, labels=labels)
        self.eval_loss(loss=eval_loss, num_measurements=log_probs.shape[0] * log_probs.shape[1])
        translations = [self.decoder_tokenizer.ids_to_text(tr) for tr in beam_results.cpu().numpy()]
        np_tgt = tgt_ids.cpu().numpy()
        ground_truths = [self.decoder_tokenizer.ids_to_text(tgt) for tgt in np_tgt]
        num_non_pad_tokens = np.not_equal(np_tgt, self.decoder_tokenizer.pad_id).sum().item()
        return {
            'translations': translations,
            'ground_truths': ground_truths,
            'num_non_pad_tokens': num_non_pad_tokens,
        }

    def test_step(self, batch, batch_idx):
        return self.eval_step(batch, batch_idx, 'test')

    @rank_zero_only
    def log_param_stats(self):
        for name, p in self.named_parameters():
            if p.requires_grad:
                self.trainer.logger.experiment.add_histogram(name + '_hist', p, global_step=self.global_step)
                self.trainer.logger.experiment.add_scalars(
                    name,
                    {'mean': p.mean(), 'stddev': p.std(), 'max': p.max(), 'min': p.min()},
                    global_step=self.global_step,
                )

    def validation_step(self, batch, batch_idx):
        """
        Lightning calls this inside the validation loop with the data from the validation dataloader
        passed in as `batch`.
        """
        return self.eval_step(batch, batch_idx, 'val')

    def eval_epoch_end(self, outputs, mode):
        eval_loss = self.eval_loss.compute()
        translations = list(itertools.chain(*[x['translations'] for x in outputs]))
        ground_truths = list(itertools.chain(*[x['ground_truths'] for x in outputs]))

        # TODO: add target language so detokenizer can be lang specific.
        detokenizer = MosesDetokenizer(lang=self.tgt_language)

        if self.tgt_language in ['ja']:
            sp_detokenizer = SentencePieceDetokenizer()
            translations = [sp_detokenizer.detokenize(sent.split()) for sent in translations]
            ground_truths = [sp_detokenizer.detokenize(sent.split()) for sent in ground_truths]

        if not self.tgt_language in ['zh']:
            translations = [detokenizer.detokenize(sent.split()) for sent in translations]
            ground_truths = [detokenizer.detokenize(sent.split()) for sent in ground_truths]
        else:
            zh_detokenizer = PanguJiebaDetokenizer()
            translations = [zh_detokenizer.detokenize(sent) for sent in translations]
            ground_truths = [zh_detokenizer.detokenize(sent) for sent in ground_truths]
        assert len(translations) == len(ground_truths)
        if self.tgt_language in ['ja']:
            sacre_bleu = corpus_bleu(translations, [ground_truths], tokenize="ja-mecab")
        elif self.tgt_language in ['zh']:
            sacre_bleu = corpus_bleu(translations, [ground_truths], tokenize="zh")
        else:
            sacre_bleu = corpus_bleu(translations, [ground_truths], tokenize="13a")

        dataset_name = "Validation" if mode == 'val' else "Test"
        logging.info(f"\n\n\n\n{dataset_name} set size: {len(translations)}")
        logging.info(f"{dataset_name} Sacre BLEU = {sacre_bleu.score}")
        logging.info(f"{dataset_name} TRANSLATION EXAMPLES:".upper())
        for i in range(0, 3):
            ind = random.randint(0, len(translations) - 1)
            logging.info("    " + '\u0332'.join(f"EXAMPLE {i}:"))
            logging.info(f"    Prediction:   {translations[ind]}")
            logging.info(f"    Ground Truth: {ground_truths[ind]}")

        ans = {f"{mode}_loss": eval_loss, f"{mode}_sacreBLEU": sacre_bleu.score}
        ans['log'] = dict(ans)
        return ans

    def validation_epoch_end(self, outputs):
        """
        Called at the end of validation to aggregate outputs.
        :param outputs: list of individual outputs of each validation step.
        """
        self.log_dict(self.eval_epoch_end(outputs, 'val'))

    def test_epoch_end(self, outputs):
        return self.eval_epoch_end(outputs, 'test')

    def setup_enc_dec_tokenizers(
        self,
        encoder_tokenizer_library=None,
        encoder_tokenizer_model=None,
        encoder_bpe_dropout=0.0,
        encoder_model_name=None,
        decoder_tokenizer_library=None,
        decoder_tokenizer_model=None,
        decoder_bpe_dropout=0.0,
        decoder_model_name=None,
    ):

        supported_tokenizers = ['yttm', 'huggingface']
        if (
            encoder_tokenizer_library not in supported_tokenizers
            or decoder_tokenizer_library not in supported_tokenizers
        ):
            raise NotImplementedError(f"Currently we only support tokenizers in {supported_tokenizers}.")

        self.encoder_tokenizer = get_nmt_tokenizer(
            library=encoder_tokenizer_library,
            tokenizer_model=self.register_artifact("cfg.encoder_tokenizer.tokenizer_model", encoder_tokenizer_model),
            bpe_dropout=encoder_bpe_dropout,
            model_name=encoder_model_name,
            vocab_file=None,
            special_tokens=None,
            use_fast=False,
        )
        self.decoder_tokenizer = get_nmt_tokenizer(
            library=decoder_tokenizer_library,
            tokenizer_model=self.register_artifact("cfg.decoder_tokenizer.tokenizer_model", decoder_tokenizer_model),
            bpe_dropout=decoder_bpe_dropout,
            model_name=decoder_model_name,
            vocab_file=None,
            special_tokens=None,
            use_fast=False,
        )

    def setup_training_data(self, train_data_config: Optional[DictConfig]):
        self._train_dl = self._setup_dataloader_from_config(cfg=train_data_config)

    def setup_validation_data(self, val_data_config: Optional[DictConfig]):
        self._validation_dl = self._setup_dataloader_from_config(cfg=val_data_config)

    def setup_test_data(self, test_data_config: Optional[DictConfig]):
        self._test_dl = self._setup_dataloader_from_config(cfg=test_data_config)

    def _setup_dataloader_from_config(self, cfg: DictConfig):
        if cfg.get("load_from_cached_dataset", False):
            logging.info('Loading from cached dataset %s' % (cfg.src_file_name))
            if cfg.src_file_name != cfg.tgt_file_name:
                raise ValueError("src must be equal to target for cached dataset")
            dataset = pickle.load(open(cfg.src_file_name, 'rb'))
            dataset.reverse_lang_direction = cfg.get("reverse_lang_direction", False)
        elif cfg.get("use_tarred_dataset", False):
            if cfg.get('tar_files') is None:
                raise FileNotFoundError("Could not find tarred dataset.")
            logging.info(f'Loading from tarred dataset {cfg.get("tar_files")}')
            if cfg.get("metadata_file", None) is None:
                raise FileNotFoundError("Could not find metadata path in config")
            dataset = TarredTranslationDataset(
                text_tar_filepaths=cfg.tar_files,
                metadata_path=cfg.metadata_file,
                encoder_tokenizer=self.encoder_tokenizer,
                decoder_tokenizer=self.decoder_tokenizer,
                shuffle_n=cfg.get("tar_shuffle_n", 100),
                shard_strategy=cfg.get("shard_strategy", "scatter"),
                global_rank=self.global_rank,
                world_size=self.world_size,
                reverse_lang_direction=cfg.get("reverse_lang_direction", False),
            )
            return torch.utils.data.DataLoader(
                dataset=dataset,
                batch_size=1,
                num_workers=cfg.get("num_workers", 2),
                pin_memory=cfg.get("pin_memory", False),
                drop_last=cfg.get("drop_last", False),
            )
        else:
            dataset = TranslationDataset(
                dataset_src=str(Path(cfg.src_file_name).expanduser()),
                dataset_tgt=str(Path(cfg.tgt_file_name).expanduser()),
                tokens_in_batch=cfg.tokens_in_batch,
                clean=cfg.get("clean", False),
                max_seq_length=cfg.get("max_seq_length", 512),
                min_seq_length=cfg.get("min_seq_length", 1),
                max_seq_length_diff=cfg.get("max_seq_length_diff", 512),
                max_seq_length_ratio=cfg.get("max_seq_length_ratio", 512),
                cache_ids=cfg.get("cache_ids", False),
                cache_data_per_node=cfg.get("cache_data_per_node", False),
                use_cache=cfg.get("use_cache", False),
                reverse_lang_direction=cfg.get("reverse_lang_direction", False),
            )
            dataset.batchify(self.encoder_tokenizer, self.decoder_tokenizer)
        if cfg.shuffle:
            sampler = pt_data.RandomSampler(dataset)
        else:
            sampler = pt_data.SequentialSampler(dataset)
        return torch.utils.data.DataLoader(
            dataset=dataset,
            batch_size=1,
            sampler=sampler,
            num_workers=cfg.get("num_workers", 2),
            pin_memory=cfg.get("pin_memory", False),
            drop_last=cfg.get("drop_last", False),
        )

    def replace_beam_with_sampling(self, topk=500):
        self.beam_search = TopKSequenceGenerator(
            embedding=self.decoder.embedding,
            decoder=self.decoder.decoder,
            log_softmax=self.log_softmax,
            max_sequence_length=self.beam_search.max_seq_length,
            beam_size=topk,  # hyperparam from https://arxiv.org/pdf/1808.09381.pdf
            bos=self.decoder_tokenizer.bos_id,
            pad=self.decoder_tokenizer.pad_id,
            eos=self.decoder_tokenizer.eos_id,
        )

    def get_normalizer_and_tokenizer(self, lang):
        """
        Returns a normalizer and tokenizer for a specific language.
        """
        tokenizer, normalizer = None, None
        if lang not in ['zh', 'ja']:
            tokenizer = MosesTokenizer(lang=lang)
            normalizer = MosesPunctNormalizer(lang=lang)
        elif lang == 'ja':
            raise NotImplementedError("Input tokenization for Japanese is not implemented yet")
        elif lang == 'zh':
            normalizer = opencc.OpenCC('t2s.json')

        return tokenizer, normalizer

    def get_detokenizer(self, lang):
        """
        Returns a detokenizer for a specific language.
        """
        detokenizer = None
        if lang not in ['zh']:
            detokenizer = MosesDetokenizer(lang=lang)
        elif lang == 'zh':
            detokenizer = PanguJiebaDetokenizer()

        return detokenizer

    @torch.no_grad()
    def batch_translate(
        self, src: torch.LongTensor, src_mask: torch.LongTensor, source_lang: str = None, target_lang: str = None
    ) -> List[str]:
        """
        Translates a minibatch of inputs from source language to target language.
        Args:
            src: minibatch of inputs in the src language (batch x seq_len)
            src_mask: mask tensor indicating elements to be ignored (batch x seq_len)
            target_lang: if not None, corresponding Detokenizer will be run
        Returns:
            translations: a list strings containing detokenized translations
            inputs: a list of string containing detokenized inputs
        """
        src_hiddens = self.encoder(input_ids=src, encoder_mask=src_mask)
        beam_results = self.beam_search(encoder_hidden_states=src_hiddens, encoder_input_mask=src_mask)
        beam_results = self.filter_predicted_ids(beam_results)

        target_detokenizer = self.get_detokenizer(target_lang)
        source_detokenizer = self.get_detokenizer(source_lang)

        if target_lang or source_lang == 'ja':
            sp_detokenizer = SentencePieceDetokenizer()

        translations = [self.decoder_tokenizer.ids_to_text(tr) for tr in beam_results.cpu().numpy()]
        inputs = [self.encoder_tokenizer.ids_to_text(inp) for inp in src.cpu().numpy()]

        if target_detokenizer is not None:
            if target_lang == 'ja':
                translations = [sp_detokenizer.detokenize(translation.split()) for translation in translations]
            translations = [target_detokenizer.detokenize(translation.split()) for translation in translations]

        if source_detokenizer is not None:
            if target_lang == 'ja':
                inputs = [sp_detokenizer.detokenize(inp.split()) for inp in inputs]
            inputs = [source_detokenizer.detokenize(item.split()) for item in inputs]

        return inputs, translations

    @torch.no_grad()
    def translate(self, text: List[str], source_lang: str = None, target_lang: str = None) -> List[str]:
        """
        Translates list of sentences from source language to target language.
        Should be regular text, this method performs its own tokenization/de-tokenization
        Args:
            text: list of strings to translate
            source_lang: if not None, corresponding Tokenizer and Normalizer will be run
            target_lang: if not None, corresponding Detokenizer will be run
        Returns:
            list of translated strings
        """
        if source_lang is None:
            source_lang = self.src_language
        if target_lang is None:
            target_lang = self.tgt_language

        mode = self.training

        tokenizer, normalizer = self.get_normalizer_and_tokenizer(source_lang)
        detokenizer = self.get_detokenizer(target_lang)

        try:
            self.eval()
            res = []
            for txt in text:
                if source_lang != "None":
                    if source_lang == "zh":
                        txt = normalizer.convert(txt)
                        txt = ' '.join(jieba.cut(txt))
                    else:
                        txt = normalizer.normalize(txt)
                        txt = tokenizer.tokenize(txt, escape=False, return_str=True)
                ids = self.encoder_tokenizer.text_to_ids(txt)
                ids = [self.encoder_tokenizer.bos_id] + ids + [self.encoder_tokenizer.eos_id]
                src = torch.Tensor(ids).long().to(self._device).unsqueeze(0)
                src_mask = torch.ones_like(src)
                src_hiddens = self.encoder(input_ids=src, encoder_mask=src_mask)
                beam_results = self.beam_search(encoder_hidden_states=src_hiddens, encoder_input_mask=src_mask)
                beam_results = self.filter_predicted_ids(beam_results)
                translation_ids = beam_results.cpu()[0].numpy()
                translation = self.decoder_tokenizer.ids_to_text(translation_ids)
                if target_lang == 'ja':
                    sp_detokenizer = SentencePieceDetokenizer()
                    translation = sp_detokenizer.detokenize(translation.split())
                translation = detokenizer.detokenize(translation.split())
                res.append(translation)
        finally:
            self.train(mode=mode)
        return res

    @classmethod
    def list_available_models(cls) -> Optional[Dict[str, str]]:
        pass

    def configure_ddp(self, model: LightningModule, device_ids: List[int]) -> DistributedDataParallel:
        logging.info(f'overriding ddp to set find_unused_parameters to {self._cfg.find_unused_parameters}')
        model = LightningDistributedDataParallel(
            model, device_ids=device_ids, find_unused_parameters=self._cfg.find_unused_parameters
        )
        return model

    def setup(self, stage):
        if stage == "fit":
            # Update PTL trainer to use our configure_ddp
            self._trainer.accelerator_backend.ddp_plugin.configure_ddp = self.configure_ddp
