import time
import os

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import torch
import torchvision.transforms.functional as F

from .datasets import BojaDataSet
from .._file_utils import get_highest_numbered_file
from .._image_utils import draw_bboxes
from .. import _models
from .._s3_utils import s3_bucket_exists, s3_download_highest_numbered_file
from .._settings import (
    DEFAULT_LOCAL_DATA_DIR,
    DEFAULT_S3_DATA_DIR,
    LABEL_FILE_NAME,
    IMAGE_DIR_NAME,
    ANNOTATION_DIR_NAME,
    MANIFEST_DIR_NAME,
    MODEL_STATE_DIR_NAME,
    MANIFEST_FILE_TYPE,
    MODEL_STATE_FILE_TYPE,
    NETWORKS,
)

matplotlib.use("TKAgg")


def get_newest_manifest_path(manifest_dir_path: str) -> str:
    return get_highest_numbered_file(manifest_dir_path, MANIFEST_FILE_TYPE)


def get_newest_saved_model_path(model_dir_path: str, filter_keyword=None) -> str:
    return get_highest_numbered_file(
        model_dir_path, MODEL_STATE_FILE_TYPE, filter_keyword
    )


def main(args):

    use_s3 = True if args.s3_bucket_name is not None else False

    if use_s3:
        if not s3_bucket_exists(args.s3_bucket_name):
            use_s3 = False
            print(
                "Bucket: %s either does not exist or you do not have access to it"
                % args.s3_bucket_name
            )
        else:
            print("Bucket: %s exists and you have access to it" % args.s3_bucket_name)

    if use_s3:
        # Get the newest model
        s3_download_highest_numbered_file(
            args.s3_bucket_name,
            "/".join([args.s3_data_dir, MODEL_STATE_DIR_NAME]),
            os.path.join(args.local_data_dir, MODEL_STATE_DIR_NAME),
            MODEL_STATE_FILE_TYPE,
            args.network,
        )

    label_file_path = os.path.join(args.local_data_dir, LABEL_FILE_NAME)
    if not os.path.isfile(label_file_path):
        print("Missing file %s" % label_file_path)
        return

    # read in the category labels
    labels = open(label_file_path).read().splitlines()

    if len(labels) == 0:
        print("No label categories found in %s" % label_file_path)
        return

    # Add the background as the first class
    labels.insert(0, "background")

    print("Labels found:")
    print(labels)

    manifest_file_path = (
        args.manifest_path
        if args.manifest_path is not None
        else get_newest_manifest_path(
            os.path.join(args.local_data_dir, MANIFEST_DIR_NAME)
        )
    )

    if manifest_file_path is None:
        print("No manifest file found")
        return

    saved_model_file_path = (
        args.model_path
        if args.model_path is not None
        else get_newest_saved_model_path(
            os.path.join(args.local_data_dir, MODEL_STATE_DIR_NAME), args.network,
        )
    )

    if saved_model_file_path is None:
        print("No saved model state found")
        return

    # train on the GPU or on the CPU, if a GPU is not available
    device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")

    print("Using device: ", device)

    # Add one class for the background
    num_classes = len(labels)
    # use our dataset and defined transformations
    dataset = BojaDataSet(
        os.path.join(args.local_data_dir, IMAGE_DIR_NAME),
        os.path.join(args.local_data_dir, ANNOTATION_DIR_NAME),
        manifest_file_path,
        labels,
        training=False,
    )

    # get the model using our helper function
    model = _models.__dict__[args.network](
        num_classes,
        box_score_thresh=args.threshold,
        min_size=600,
        max_size=800,
        box_nms_thresh=0.3,
    )

    print("Loading model state from: %s" % saved_model_file_path)

    checkpoint = torch.load(saved_model_file_path, map_location=device)
    model.load_state_dict(checkpoint["model"])

    print("Model state loaded")

    model.eval()

    # move model to the right device
    model.to(device)

    # create plots
    fig, (ground_truth_ax, inference_ax) = plt.subplots(1, 2)

    fig.canvas.set_window_title("Visualize")

    label_colors = plt.get_cmap("hsv")(np.linspace(0, 0.9, len(labels)))

    for data in dataset:
        image, target = data
        # make a copy of the image for display before sending to device
        display_image_base = F.to_pil_image(image)
        image = image.to(device)
        target = {k: v.to(device) for k, v in target.items()}
        model_time = time.time()

        outputs = []
        with torch.no_grad():
            outputs = model([image])
        outputs = [
            {k: v.to(torch.device("cpu")) for k, v in t.items()} for t in outputs
        ]
        model_time = time.time() - model_time
        print("Inference time = ", model_time)

        ground_truth_ax.clear()
        inference_ax.clear()

        ground_truth_ax.set_title("Ground Truth")
        inference_ax.set_title("Inference")

        ground_truth_ax.imshow(display_image_base)
        inference_ax.imshow(display_image_base)

        draw_bboxes(
            ground_truth_ax, target["boxes"], target["labels"], labels, label_colors
        )

        # filter out the background labels and scores bellow threshold
        filtered_output = [
            (outputs[0]["boxes"][j], outputs[0]["labels"][j], outputs[0]["scores"][j],)
            for j in range(len(outputs[0]["boxes"]))
            if outputs[0]["scores"][j] > args.threshold and outputs[0]["labels"][j] > 0
        ]

        inference_boxes, inference_labels, inference_scores = (
            zip(*filtered_output) if len(filtered_output) > 0 else ([], [], [])
        )

        draw_bboxes(
            inference_ax,
            inference_boxes,
            inference_labels,
            labels,
            label_colors,
            inference_scores,
        )

        plt.pause(0.001)

    # evaluate on the test dataset
    #    evaluate(model, data_loader, device=device)

    print("Visualization complete")


if __name__ == "__main__":

    import argparse

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--local_data_dir",
        type=str,
        default=DEFAULT_LOCAL_DATA_DIR,
        help="Local data directory.",
    )
    parser.add_argument(
        "--manifest_path", type=str,
    )
    parser.add_argument("--model_path", type=str)
    parser.add_argument(
        "--s3_bucket_name", type=str,
    )
    parser.add_argument(
        "--s3_data_dir",
        type=str,
        default=DEFAULT_S3_DATA_DIR,
        help="Prefix of the s3 data objects.",
    )
    parser.add_argument(
        "--network",
        type=str,
        choices=NETWORKS,
        default=NETWORKS[0],
        help="The neural network to use for object detection",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.5,
        help="The threshold above which to display predicted bounding boxes",
    )

    args = parser.parse_args()

    main(args)

