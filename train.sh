python train.py \
        --cuda \
        -d coco \
        -v yolof_r50_C5_1x \
        --norm GN \
        --batch_size 15 \
        --img_size 800 \
        --lr 0.01 \
        --lr_backbone 0.01 \
        --wp_iter 500 \
        --optimizer sgd \
        --accumulate 4