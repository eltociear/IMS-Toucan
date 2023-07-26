import torch
import torch.multiprocessing
import wandb
from torch.nn.utils.rnn import pad_sequence
from torch.utils.data.dataloader import DataLoader
from tqdm import tqdm

from EmbeddingModel.StyleEmbedding import StyleEmbedding
from Utility.WarmupScheduler import ToucanWarmupScheduler as WarmupScheduler
from Utility.path_to_transcript_dicts import *
from Utility.utils import delete_old_checkpoints
from Utility.utils import get_most_recent_checkpoint
from Utility.utils import plot_progress_spec_toucantts
from run_weight_averaging import average_checkpoints
from run_weight_averaging import get_n_recent_checkpoints_paths
from run_weight_averaging import load_net_toucan
from run_weight_averaging import save_model_for_use


def collate_and_pad(batch):
    # text, text_len, speech, speech_len, durations, energy, pitch, utterance condition, language_id
    return (pad_sequence([datapoint[0] for datapoint in batch], batch_first=True),
            torch.stack([datapoint[1] for datapoint in batch]).squeeze(1),
            pad_sequence([datapoint[2] for datapoint in batch], batch_first=True),
            torch.stack([datapoint[3] for datapoint in batch]).squeeze(1),
            pad_sequence([datapoint[4] for datapoint in batch], batch_first=True),
            pad_sequence([datapoint[5] for datapoint in batch], batch_first=True),
            pad_sequence([datapoint[6] for datapoint in batch], batch_first=True),
            pad_sequence([datapoint[7] for datapoint in batch], batch_first=True),
            torch.stack([datapoint[8] for datapoint in batch]))


def train_loop(net,
               datasets,
               device,
               save_directory,
               batch_size,
               steps,
               steps_per_checkpoint,
               lr,
               path_to_checkpoint,
               lang,
               path_to_embed_model,
               resume,
               fine_tune,
               warmup_steps,
               use_wandb,
               ):
    """
    see train loop arbiter for explanations of the arguments
    """
    net = net.to(device)

    if steps % steps_per_checkpoint == 0:
        steps = steps + 1
    else:
        steps = steps + ((steps_per_checkpoint + 1) - (steps % steps_per_checkpoint))  # making sure to stop at the closest point that makes sense to the specified stopping point

    style_embedding_function = StyleEmbedding().to(device)
    check_dict = torch.load(path_to_embed_model, map_location=device)
    style_embedding_function.load_state_dict(check_dict["style_emb_func"])
    style_embedding_function.eval()
    style_embedding_function.requires_grad_(False)

    torch.multiprocessing.set_sharing_strategy('file_system')
    train_loaders = list()
    train_iters = list()
    for dataset in datasets:
        train_loaders.append(DataLoader(batch_size=1,
                                        dataset=dataset,
                                        drop_last=True,
                                        num_workers=2,
                                        pin_memory=True,
                                        shuffle=True,
                                        prefetch_factor=4,
                                        collate_fn=collate_and_pad,
                                        persistent_workers=True))
        train_iters.append(iter(train_loaders[-1]))
    optimizer = torch.optim.AdamW(net.parameters(), lr=lr)
    scheduler = WarmupScheduler(optimizer, peak_lr=lr, warmup_steps=warmup_steps, max_steps=steps)
    steps_run_previously = 0
    regression_losses_total = list()
    glow_losses_total = list()
    duration_losses_total = list()
    pitch_losses_total = list()
    energy_losses_total = list()

    if resume:
        path_to_checkpoint = get_most_recent_checkpoint(checkpoint_dir=save_directory)
    if path_to_checkpoint is not None:
        check_dict = torch.load(path_to_checkpoint, map_location=device)
        net.load_state_dict(check_dict["model"])
        if not fine_tune:
            optimizer.load_state_dict(check_dict["optimizer"])
            scheduler.load_state_dict(check_dict["scheduler"])
            steps_run_previously = check_dict["step_counter"]
        if steps_run_previously > steps:
            print("Desired steps already reached in loaded checkpoint.")
            return

    net.train()
    # =============================
    # Actual train loop starts here
    # =============================
    for step_counter in tqdm(range(steps_run_previously, steps)):
        batches = []
        while len(batches) < batch_size:
            for index in random.sample(list(range(len(datasets))), len(datasets)):
                if len(batches) < batch_size:
                    # we get one batch for each task (i.e. language in this case) in a randomized order
                    try:
                        batch = next(train_iters[index])
                        batches.append(batch)
                    except StopIteration:
                        train_iters[index] = iter(train_loaders[index])
                        batch = next(train_iters[index])
                        batches.append(batch)
        batch = collate_and_pad(batches)

        text_tensors = batch[0].to(device)
        text_lengths = batch[1].squeeze().to(device)
        gold_speech = batch[2].to(device)
        speech_lengths = batch[3].squeeze().to(device)
        gold_durations = batch[4].to(device)
        gold_pitch = batch[6].unsqueeze(-1).to(device)  # mind the switched order
        gold_energy = batch[5].unsqueeze(-1).to(device)  # mind the switched order
        lang_ids = batch[8].squeeze(1).to(device)

        train_loss = 0.0
        # we sum the loss for each task, as we would do for the
        # second order regular MAML, but we do it only over one
        # step (i.e. iterations of inner loop = 1)
        style_embedding = style_embedding_function(batch_of_feature_sequences=gold_speech,
                                                   batch_of_feature_sequence_lengths=speech_lengths)
        regression_loss, glow_loss, duration_loss, pitch_loss, energy_loss = net(
            text_tensors=text_tensors,
            text_lengths=text_lengths,
            gold_speech=gold_speech,
            speech_lengths=speech_lengths,
            gold_durations=gold_durations,
            gold_pitch=gold_pitch,
            gold_energy=gold_energy,
            utterance_embedding=style_embedding,
            lang_ids=lang_ids,
            return_feats=False)

        # then we directly update our meta-parameters without
        # the need for any task specific parameters

        if not torch.isnan(regression_loss):
            train_loss = train_loss + regression_loss
        if not torch.isnan(glow_loss):
            train_loss = train_loss + glow_loss
        if not torch.isnan(duration_loss):
            train_loss = train_loss + duration_loss
        if not torch.isnan(pitch_loss):
            train_loss = train_loss + pitch_loss
        if not torch.isnan(energy_loss):
            train_loss = train_loss + energy_loss

        regression_losses_total.append(regression_loss.item())
        glow_losses_total.append(glow_loss.item())
        duration_losses_total.append(duration_loss.item())
        pitch_losses_total.append(pitch_loss.item())
        energy_losses_total.append(energy_loss.item())

        optimizer.zero_grad()
        train_loss.backward()
        torch.nn.utils.clip_grad_norm_(net.parameters(), 1.0, error_if_nonfinite=False)
        optimizer.step()
        scheduler.step()

        if step_counter % steps_per_checkpoint == 0 and step_counter != 0:
            # ==============================
            # Enough steps for some insights
            # ==============================
            net.eval()
            style_embedding_function.eval()
            default_embedding = style_embedding_function(
                batch_of_feature_sequences=datasets[0][0][2].unsqueeze(0).to(device),
                batch_of_feature_sequence_lengths=datasets[0][0][3].unsqueeze(0).to(device)).squeeze()
            print("Reconstruction Loss:    {}".format(round(sum(regression_losses_total) / len(regression_losses_total), 3)))
            print("Steps:                  {}\n".format(step_counter))
            torch.save({
                "model"       : net.state_dict(),
                "optimizer"   : optimizer.state_dict(),
                "scheduler"   : scheduler.state_dict(),
                "step_counter": step_counter,
                "default_emb" : default_embedding,
                "config"      : net.config
            },
                os.path.join(save_directory, "checkpoint_{}.pt".format(step_counter)))
            delete_old_checkpoints(save_directory, keep=5)

            if use_wandb:
                wandb.log({
                    "l1_criterion" : round(sum(regression_losses_total) / len(regression_losses_total), 5),
                    "glow_loss"    : round(sum(glow_losses_total) / len(glow_losses_total), 5),
                    "duration_loss": round(sum(duration_losses_total) / len(duration_losses_total), 5),
                    "pitch_loss"   : round(sum(pitch_losses_total) / len(pitch_losses_total), 5),
                    "energy_loss"  : round(sum(energy_losses_total) / len(energy_losses_total), 5),
                }, step=step_counter)

            try:
                path_to_most_recent_plot = plot_progress_spec_toucantts(net,
                                                                        device,
                                                                        save_dir=save_directory,
                                                                        step=step_counter,
                                                                        lang=lang,
                                                                        default_emb=default_embedding)
                if use_wandb:
                    wandb.log({
                        "progress_plot": wandb.Image(path_to_most_recent_plot)
                    }, step=step_counter)

            except IndexError:
                print("generating progress plots failed.")

            regression_losses_total = list()
            glow_losses_total = list()
            duration_losses_total = list()
            pitch_losses_total = list()
            energy_losses_total = list()

            if step_counter > steps * 4 / 5:
                # Run manual SWA (torch builtin doesn't work unfortunately due to the use of weight norm in the postflow)
                checkpoint_paths = get_n_recent_checkpoints_paths(checkpoint_dir=save_directory, n=2)
                averaged_model, default_embed = average_checkpoints(checkpoint_paths, load_func=load_net_toucan)
                save_model_for_use(model=averaged_model, default_embed=default_embed, name=os.path.join(save_directory, "best.pt"))
                check_dict = torch.load(os.path.join(save_directory, "best.pt"), map_location=device)
                net.load_state_dict(check_dict["model"])

            net.train()
