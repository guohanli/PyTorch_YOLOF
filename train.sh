python train.py \
        --cuda \
        -d coco \
        -v yolof_r50_C5_1x \
        --batch_size 16 \
        --img_size 800 \
        --lr 0.03 \
        --wp_iter 500 \
        --accumulate 1