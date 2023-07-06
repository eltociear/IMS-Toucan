import torch
from torch.nn import Linear
from torch.nn import Sequential
from torch.nn import Tanh

from Layers.Conformer import Conformer
from Layers.DurationPredictor import DurationPredictor
from Layers.LengthRegulator import LengthRegulator
from Layers.VariancePredictor import VariancePredictor
from Preprocessing.articulatory_features import get_feature_to_index_lookup
from TTSTrainingInterfaces.ToucanTTS.Glow import Glow
from Utility.utils import make_non_pad_mask


class ToucanTTS(torch.nn.Module):

    def __init__(self,
                 # network structure related
                 input_feature_dimensions=62,
                 output_spectrogram_channels=72,
                 attention_dimension=192,
                 attention_heads=4,
                 positionwise_conv_kernel_size=1,
                 use_scaled_positional_encoding=True,
                 use_macaron_style_in_conformer=True,
                 use_cnn_in_conformer=True,

                 # encoder
                 encoder_layers=6,
                 encoder_units=1536,
                 encoder_normalize_before=True,
                 encoder_concat_after=False,
                 conformer_encoder_kernel_size=7,
                 transformer_enc_dropout_rate=0.2,
                 transformer_enc_positional_dropout_rate=0.2,
                 transformer_enc_attn_dropout_rate=0.2,

                 # decoder
                 decoder_layers=6,
                 decoder_units=1536,
                 decoder_concat_after=False,
                 conformer_decoder_kernel_size=31,
                 decoder_normalize_before=True,
                 transformer_dec_dropout_rate=0.2,
                 transformer_dec_positional_dropout_rate=0.2,
                 transformer_dec_attn_dropout_rate=0.2,

                 # duration predictor
                 duration_predictor_layers=5,
                 duration_predictor_kernel_size=7,
                 duration_predictor_dropout_rate=0.2,

                 # pitch predictor
                 pitch_predictor_layers=7,
                 pitch_predictor_kernel_size=7,
                 pitch_predictor_dropout=0.5,
                 pitch_embed_kernel_size=1,
                 pitch_embed_dropout=0.0,

                 # energy predictor
                 energy_predictor_layers=2,
                 energy_predictor_kernel_size=3,
                 energy_predictor_dropout=0.5,
                 energy_embed_kernel_size=1,
                 energy_embed_dropout=0.0,

                 # additional features
                 utt_embed_dim=192,
                 detach_postflow=True,
                 lang_embs=8000,
                 weights=None,
                 use_conditional_layernorm_embedding_integration=False,
                 n_codebooks=9,
                 codebook_dim=8
                 ):
        super().__init__()

        self.input_feature_dimensions = input_feature_dimensions
        self.output_spectrogram_channels = output_spectrogram_channels
        self.attention_dimension = attention_dimension
        self.detach_postflow = detach_postflow
        self.use_scaled_pos_enc = use_scaled_positional_encoding
        self.multilingual_model = lang_embs is not None
        self.multispeaker_model = utt_embed_dim is not None
        self.codebook_dim = codebook_dim

        articulatory_feature_embedding = Sequential(Linear(input_feature_dimensions, 100), Tanh(), Linear(100, attention_dimension))
        self.encoder = Conformer(conformer_type="encoder",
                                 attention_dim=attention_dimension,
                                 attention_heads=attention_heads,
                                 linear_units=encoder_units,
                                 num_blocks=encoder_layers,
                                 input_layer=articulatory_feature_embedding,
                                 dropout_rate=transformer_enc_dropout_rate,
                                 positional_dropout_rate=transformer_enc_positional_dropout_rate,
                                 attention_dropout_rate=transformer_enc_attn_dropout_rate,
                                 normalize_before=encoder_normalize_before,
                                 concat_after=encoder_concat_after,
                                 positionwise_conv_kernel_size=positionwise_conv_kernel_size,
                                 macaron_style=use_macaron_style_in_conformer,
                                 use_cnn_module=use_cnn_in_conformer,
                                 cnn_module_kernel=conformer_encoder_kernel_size,
                                 zero_triu=False,
                                 utt_embed=utt_embed_dim,
                                 lang_embs=lang_embs,
                                 use_output_norm=True,
                                 use_conditional_layernorm_embedding_integration=use_conditional_layernorm_embedding_integration)

        self.duration_predictor = DurationPredictor(idim=attention_dimension,
                                                    n_layers=duration_predictor_layers,
                                                    n_chans=attention_dimension,
                                                    kernel_size=duration_predictor_kernel_size,
                                                    dropout_rate=duration_predictor_dropout_rate,
                                                    utt_embed_dim=utt_embed_dim,
                                                    use_conditional_layernorm_embedding_integration=use_conditional_layernorm_embedding_integration)

        self.pitch_predictor = VariancePredictor(idim=attention_dimension,
                                                 n_layers=pitch_predictor_layers,
                                                 n_chans=attention_dimension,
                                                 kernel_size=pitch_predictor_kernel_size,
                                                 dropout_rate=pitch_predictor_dropout,
                                                 utt_embed_dim=utt_embed_dim,
                                                 use_conditional_layernorm_embedding_integration=use_conditional_layernorm_embedding_integration)

        self.energy_predictor = VariancePredictor(idim=attention_dimension,
                                                  n_layers=energy_predictor_layers,
                                                  n_chans=attention_dimension,
                                                  kernel_size=energy_predictor_kernel_size,
                                                  dropout_rate=energy_predictor_dropout,
                                                  utt_embed_dim=utt_embed_dim,
                                                  use_conditional_layernorm_embedding_integration=use_conditional_layernorm_embedding_integration)

        self.pitch_embed = Sequential(torch.nn.Conv1d(in_channels=1,
                                                      out_channels=attention_dimension,
                                                      kernel_size=pitch_embed_kernel_size,
                                                      padding=(pitch_embed_kernel_size - 1) // 2),
                                      torch.nn.Dropout(pitch_embed_dropout))

        self.energy_embed = Sequential(torch.nn.Conv1d(in_channels=1,
                                                       out_channels=attention_dimension,
                                                       kernel_size=energy_embed_kernel_size,
                                                       padding=(energy_embed_kernel_size - 1) // 2),
                                       torch.nn.Dropout(energy_embed_dropout))

        self.length_regulator = LengthRegulator()

        self.decoder = Conformer(conformer_type="decoder",
                                 attention_dim=attention_dimension,
                                 attention_heads=attention_heads,
                                 linear_units=decoder_units,
                                 num_blocks=decoder_layers,
                                 input_layer=None,
                                 dropout_rate=transformer_dec_dropout_rate,
                                 positional_dropout_rate=transformer_dec_positional_dropout_rate,
                                 attention_dropout_rate=transformer_dec_attn_dropout_rate,
                                 normalize_before=decoder_normalize_before,
                                 concat_after=decoder_concat_after,
                                 positionwise_conv_kernel_size=positionwise_conv_kernel_size,
                                 macaron_style=use_macaron_style_in_conformer,
                                 use_cnn_module=use_cnn_in_conformer,
                                 cnn_module_kernel=conformer_decoder_kernel_size,
                                 use_output_norm=False,
                                 utt_embed=utt_embed_dim,
                                 use_conditional_layernorm_embedding_integration=use_conditional_layernorm_embedding_integration)

        self.feat_outs = torch.nn.ModuleList()
        self.codebook_decoders = torch.nn.ModuleList()
        self.post_flows = torch.nn.ModuleList()
        for _ in range(n_codebooks):
            self.codebook_decoders.append(Conformer(conformer_type="decoder",
                                                    attention_dim=attention_dimension,
                                                    attention_heads=attention_heads,
                                                    linear_units=768,
                                                    num_blocks=decoder_layers,
                                                    input_layer=None,
                                                    dropout_rate=transformer_dec_dropout_rate,
                                                    positional_dropout_rate=transformer_dec_positional_dropout_rate,
                                                    attention_dropout_rate=transformer_dec_attn_dropout_rate,
                                                    normalize_before=decoder_normalize_before,
                                                    concat_after=decoder_concat_after,
                                                    positionwise_conv_kernel_size=positionwise_conv_kernel_size,
                                                    macaron_style=use_macaron_style_in_conformer,
                                                    use_cnn_module=use_cnn_in_conformer,
                                                    cnn_module_kernel=7,
                                                    use_output_norm=False,
                                                    utt_embed=utt_embed_dim,
                                                    use_conditional_layernorm_embedding_integration=use_conditional_layernorm_embedding_integration))
            self.feat_outs.append(torch.nn.Sequential(
                Linear(attention_dimension, attention_dimension),
                torch.nn.Tanh(),
                Linear(attention_dimension, codebook_dim)))

            self.post_flows.append(Glow(in_channels=self.codebook_dim,
                                        hidden_channels=self.codebook_dim,  # post_glow_hidden
                                        kernel_size=3,  # post_glow_kernel_size
                                        dilation_rate=1,
                                        n_blocks=12,  # post_glow_n_blocks (original 12 in paper)
                                        n_layers=3,  # post_glow_n_block_layers (original 3 in paper)
                                        n_split=4,
                                        n_sqz=2,
                                        text_condition_channels=attention_dimension,
                                        share_cond_layers=False,  # post_share_cond_layers
                                        share_wn_layers=4,
                                        sigmoid_scale=False,
                                        condition_integration_projection=torch.nn.Conv1d(self.codebook_dim + utt_embed_dim, attention_dimension, 5, padding=2)
                                        ))

        self.load_state_dict(weights)
        self.eval()

    def _forward(self,
                 text_tensors,
                 text_lengths,
                 gold_durations=None,
                 gold_pitch=None,
                 gold_energy=None,
                 duration_scaling_factor=1.0,
                 utterance_embedding=None,
                 lang_ids=None,
                 pitch_variance_scale=1.0,
                 energy_variance_scale=1.0,
                 pause_duration_scaling_factor=1.0):

        if not self.multilingual_model:
            lang_ids = None

        if not self.multispeaker_model:
            utterance_embedding = None
        else:
            utterance_embedding = torch.nn.functional.normalize(utterance_embedding)

        # encoding the texts
        text_masks = make_non_pad_mask(text_lengths, device=text_lengths.device).unsqueeze(-2)
        encoded_texts, _ = self.encoder(text_tensors, text_masks, utterance_embedding=utterance_embedding, lang_ids=lang_ids)

        # predicting pitch, energy and durations
        pitch_predictions = self.pitch_predictor(encoded_texts, padding_mask=None, utt_embed=utterance_embedding) if gold_pitch is None else gold_pitch
        energy_predictions = self.energy_predictor(encoded_texts, padding_mask=None, utt_embed=utterance_embedding) if gold_energy is None else gold_energy
        predicted_durations = self.duration_predictor.inference(encoded_texts, padding_mask=None, utt_embed=utterance_embedding) if gold_durations is None else gold_durations

        # modifying the predictions with linguistic knowledge and control parameters
        for phoneme_index, phoneme_vector in enumerate(text_tensors.squeeze(0)):
            if phoneme_vector[get_feature_to_index_lookup()["voiced"]] == 0:
                pitch_predictions[0][phoneme_index] = 0.0
            if phoneme_vector[get_feature_to_index_lookup()["phoneme"]] == 0:
                energy_predictions[0][phoneme_index] = 0.0
            if phoneme_vector[get_feature_to_index_lookup()["word-boundary"]] == 1:
                predicted_durations[0][phoneme_index] = 0
            if phoneme_vector[get_feature_to_index_lookup()["silence"]] == 1 and pause_duration_scaling_factor != 1.0:
                predicted_durations[0][phoneme_index] = torch.round(predicted_durations[0][phoneme_index].float() * pause_duration_scaling_factor).long()
        if duration_scaling_factor != 1.0:
            assert duration_scaling_factor > 0
            predicted_durations = torch.round(predicted_durations.float() * duration_scaling_factor).long()
        pitch_predictions = _scale_variance(pitch_predictions, pitch_variance_scale)
        energy_predictions = _scale_variance(energy_predictions, energy_variance_scale)

        # enriching the text with pitch and energy info
        embedded_pitch_curve = self.pitch_embed(pitch_predictions.transpose(1, 2)).transpose(1, 2)
        embedded_energy_curve = self.energy_embed(energy_predictions.transpose(1, 2)).transpose(1, 2)
        enriched_encoded_texts = encoded_texts + embedded_pitch_curve + embedded_energy_curve

        # predicting durations for text and upsampling accordingly
        upsampled_enriched_encoded_texts = self.length_regulator(enriched_encoded_texts, predicted_durations)

        # decoding spectrogram
        decoded_speech, _ = self.decoder(upsampled_enriched_encoded_texts, None, utterance_embedding=utterance_embedding)

        codebook_vectors = list()
        postflowed_vectors = list()
        for index, (codebook_decoder, codebook_projector, codebook_flow) in enumerate(zip(self.codebook_decoders, self.feat_outs, self.post_flows)):
            decoded_codebook, _ = codebook_decoder(decoded_speech, None, utterance_embedding=utterance_embedding)
            codebook_vectors.append(codebook_projector(decoded_codebook).view(decoded_codebook.size(0), -1, self.codebook_dim))
            postflowed_vectors.append(codebook_flow(tgt_mels=None,
                                                    infer=True,
                                                    mel_out=codebook_vectors[-1],
                                                    encoded_texts=upsampled_enriched_encoded_texts,
                                                    tgt_nonpadding=None).squeeze())

        decoded_spectrogram = torch.cat(codebook_vectors, dim=-1)
        refined_spectrogram = torch.cat(postflowed_vectors, dim=-1)

        return decoded_spectrogram.squeeze(), refined_spectrogram.squeeze(), predicted_durations.squeeze(), pitch_predictions.squeeze(), energy_predictions.squeeze()

    @torch.inference_mode()
    def forward(self,
                text,
                durations=None,
                pitch=None,
                energy=None,
                utterance_embedding=None,
                return_duration_pitch_energy=False,
                lang_id=None,
                duration_scaling_factor=1.0,
                pitch_variance_scale=1.0,
                energy_variance_scale=1.0,
                pause_duration_scaling_factor=1.0):
        """
        Generate the sequence of spectrogram frames given the sequence of vectorized phonemes.

        Args:
            text: input sequence of vectorized phonemes
            durations: durations to be used (optional, if not provided, they will be predicted)
            pitch: token-averaged pitch curve to be used (optional, if not provided, it will be predicted)
            energy: token-averaged energy curve to be used (optional, if not provided, it will be predicted)
            return_duration_pitch_energy: whether to return the list of predicted durations for nicer plotting
            utterance_embedding: embedding of speaker information
            lang_id: id to be fed into the embedding layer that contains language information
            duration_scaling_factor: reasonable values are 0.8 < scale < 1.2.
                                     1.0 means no scaling happens, higher values increase durations for the whole
                                     utterance, lower values decrease durations for the whole utterance.
            pitch_variance_scale: reasonable values are 0.6 < scale < 1.4.
                                  1.0 means no scaling happens, higher values increase variance of the pitch curve,
                                  lower values decrease variance of the pitch curve.
            energy_variance_scale: reasonable values are 0.6 < scale < 1.4.
                                   1.0 means no scaling happens, higher values increase variance of the energy curve,
                                   lower values decrease variance of the energy curve.
            pause_duration_scaling_factor: reasonable values are 0.6 < scale < 1.4.
                                   scales the durations of pauses on top of the regular duration scaling

        Returns:
            mel spectrogram

        """
        # setup batch axis
        text_length = torch.tensor([text.shape[0]], dtype=torch.long, device=text.device)
        if durations is not None:
            durations = durations.unsqueeze(0).to(text.device)
        if pitch is not None:
            pitch = pitch.unsqueeze(0).to(text.device)
        if energy is not None:
            energy = energy.unsqueeze(0).to(text.device)
        if lang_id is not None:
            lang_id = lang_id.unsqueeze(0).to(text.device)

        before_outs, \
        after_outs, \
        predicted_durations, \
        pitch_predictions, \
        energy_predictions = self._forward(text.unsqueeze(0),
                                           text_length,
                                           gold_durations=durations,
                                           gold_pitch=pitch,
                                           gold_energy=energy,
                                           utterance_embedding=utterance_embedding.unsqueeze(0) if utterance_embedding is not None else None, lang_ids=lang_id,
                                           duration_scaling_factor=duration_scaling_factor,
                                           pitch_variance_scale=pitch_variance_scale,
                                           energy_variance_scale=energy_variance_scale,
                                           pause_duration_scaling_factor=pause_duration_scaling_factor)
        if return_duration_pitch_energy:
            return after_outs, predicted_durations, pitch_predictions, energy_predictions
        return after_outs

    def store_inverse_all(self):
        def remove_weight_norm(m):
            try:
                if hasattr(m, 'store_inverse'):
                    m.store_inverse()
                torch.nn.utils.remove_weight_norm(m)
            except ValueError:  # this module didn't have weight norm
                return

        self.apply(remove_weight_norm)


def _scale_variance(sequence, scale):
    if scale == 1.0:
        return sequence
    average = sequence[0][sequence[0] != 0.0].mean()
    sequence = sequence - average  # center sequence around 0
    sequence = sequence * scale  # scale the variance
    sequence = sequence + average  # move center back to original with changed variance
    for sequence_index in range(len(sequence[0])):
        if sequence[0][sequence_index] < 0.0:
            sequence[0][sequence_index] = 0.0
    return sequence
