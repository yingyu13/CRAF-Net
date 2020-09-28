import torch

from mmdet.core.bbox import build_assigner, PseudoSampler
from mmdet.core.utils import multi_apply
from obb.self_mmdet.core.bbox.transforms_rbbox import gt_mask_bp_obbs
from obb.self_mmdet.core.bbox import assign_and_sample, bbox2delta, dbbox2delta, dbbox2delta_v3

def cascade_target_rbbox(anchor_list,
                  valid_flag_list,
                  gt_bboxes_list,
                  gt_masks_list,
                  img_metas,
                  target_means,
                  target_stds,
                  cfg,
                  gt_bboxes_ignore_list=None,
                  gt_labels_list=None,
                  label_channels=1,
                  sampling=True,
                  unmap_outputs=True,
                  with_module=True): # hbb_trans为将水平框表示成旋转框的形式
    """Compute regression and classification targets for anchors.

    Args:
        anchor_list (list[list]): Multi level anchors of each image.
        valid_flag_list (list[list]): Multi level valid flags of each image.
        gt_bboxes_list (list[Tensor]): Ground truth bboxes of each image.
        img_metas (list[dict]): Meta info of each image.
        target_means (Iterable): Mean value of regression targets.
        target_stds (Iterable): Std value of regression targets.
        cfg (dict): RPN train configs.

    Returns:
        tuple
    """
    num_imgs = len(img_metas)
    assert len(anchor_list) == len(valid_flag_list) == num_imgs
    # print(len(valid_flag_list))
    # anchor number of multi levels
    num_level_anchors = [anchors.size(0) for anchors in anchor_list[0]]
    # concat all level anchors and flags to a single tensor
    _anchor_list = [] # 妈的其实这里很重要，不然valid_flag_list会改变的,这个作者没有考虑到
    _valid_flag_list = []
    for i in range(num_imgs):
        # print(len(valid_flag_list[i]))
        assert len(anchor_list[i]) == len(valid_flag_list[i])
        _anchor_list.append(torch.cat(anchor_list[i]))
        _valid_flag_list.append(torch.cat(valid_flag_list[i]))

    # compute targets for each image
    if gt_bboxes_ignore_list is None:
        gt_bboxes_ignore_list = [None for _ in range(num_imgs)]
    if gt_labels_list is None:
        gt_labels_list = [None for _ in range(num_imgs)]
    (all_labels, all_label_weights, all_bbox_targets, all_bbox_weights,
     pos_inds_list, neg_inds_list) = multi_apply(
         refinebox_target_rbbox_single,
         _anchor_list,
         _valid_flag_list,
         gt_bboxes_list,
         gt_masks_list,
         gt_bboxes_ignore_list,
         gt_labels_list,
         img_metas,
         target_means=target_means,
         target_stds=target_stds,
         cfg=cfg,
         label_channels=label_channels,
         sampling=sampling,
         unmap_outputs=unmap_outputs,
         with_module=with_module)
    # for item in all_label_weights:
    #     print(item.shape)
    # no valid anchors
    if any([labels is None for labels in all_labels]):
        return None
    # sampled anchors of all images
    num_total_pos = sum([max(inds.numel(), 1) for inds in pos_inds_list])
    num_total_neg = sum([max(inds.numel(), 1) for inds in neg_inds_list])
    # split targets to a list w.r.t. multiple levels
    labels_list = images_to_levels(all_labels, num_level_anchors)
    label_weights_list = images_to_levels(all_label_weights, num_level_anchors)
    bbox_targets_list = images_to_levels(all_bbox_targets, num_level_anchors)
    bbox_weights_list = images_to_levels(all_bbox_weights, num_level_anchors)
    return (labels_list, label_weights_list, bbox_targets_list,
            bbox_weights_list, num_total_pos, num_total_neg)


def images_to_levels(target, num_level_anchors):
    """Convert targets by image to targets by feature level.

    [target_img0, target_img1] -> [target_level0, target_level1, ...]
    """
    target = torch.stack(target, 0)
    level_targets = []
    start = 0
    for n in num_level_anchors:
        end = start + n
        level_targets.append(target[:, start:end].squeeze(0))
        start = end
    return level_targets


def refinebox_target_rbbox_single(flat_anchors,
                         valid_flags,
                         gt_bboxes,
                         gt_masks,
                         gt_bboxes_ignore,
                         gt_labels,
                         img_meta,
                         target_means,
                         target_stds,
                         cfg,
                         label_channels=1,
                         sampling=True,
                         unmap_outputs=True,
                         with_module=True):
    inside_flags = anchor_inside_flags(flat_anchors, valid_flags,
                                       img_meta['img_shape'][:2],
                                       cfg.allowed_border)
    if not inside_flags.any():
        return (None, ) * 6
    # assign gt and sample anchors
    # import pdb
    # print('in anchor target rbbox single')
    # pdb.set_trace()
    anchors = flat_anchors[inside_flags, :]

    if sampling:
        gt_rbboxes = gt_mask_bp_obbs(gt_masks, with_module) # 这里注意一下角度的问题
        assign_result, sampling_result = assign_and_sample(
            anchors, gt_rbboxes, gt_bboxes_ignore, gt_labels, cfg)
    else:
        gt_rbboxes = gt_mask_bp_obbs(gt_masks, with_module) # 这里注意一下角度的问题
        # print(type(gt_rbboxes))
        # print(cfg.refine_assigner.pos_iou_thr)
        bbox_assigner = build_assigner(cfg.assigner)
        assign_result = bbox_assigner.assign(anchors.detach(), gt_rbboxes,
                                             gt_bboxes_ignore, gt_labels)
        bbox_sampler = PseudoSampler()
        sampling_result = bbox_sampler.sample(assign_result, anchors,
                                              gt_bboxes)

    num_valid_anchors = anchors.shape[0]
    # anchors shape, [num_anchors, 4]
    # bbox_targets = torch.zeros_like(anchors)
    # bbox_weights = torch.zeros_like(anchors)
    bbox_targets =  torch.zeros(num_valid_anchors, 5).to(anchors.device)
    bbox_weights = torch.zeros(num_valid_anchors, 5).to(anchors.device) # 在这里用weight控制在loss里的权重

    labels = anchors.new_zeros(num_valid_anchors, dtype=torch.long)
    label_weights = anchors.new_zeros(num_valid_anchors, dtype=torch.float)

    pos_inds = sampling_result.pos_inds
    neg_inds = sampling_result.neg_inds

    # TODO: copy the code in mask target to here. trans gt_masks to gt_rbboxes
    pos_assigned_gt_inds = sampling_result.pos_assigned_gt_inds
    # implementation A
    # pos_gt_masks = gt_masks[pos_assigned_gt_inds.cpu().numpy()]
    # pos_gt_obbs = gt_mask_bp_obbs(pos_gt_masks)
    # pos_gt_obbs_ts = torch.from_numpy(pos_gt_obbs).to(sampling_result.pos_bboxes.device)
    # implementation B
    gt_obbs = gt_mask_bp_obbs(gt_masks, with_module)
    gt_obbs_ts = torch.from_numpy(gt_obbs).to(sampling_result.pos_bboxes.device)
    pos_gt_obbs_ts = gt_obbs_ts[pos_assigned_gt_inds]
    if len(pos_inds) > 0:
        # pos_bbox_targets = bbox2delta(sampling_result.pos_bboxes,
        #                               sampling_result.pos_gt_bboxes,
        #                               target_means, target_stds)
        # if hbb_trans == 'hbb2obb':
        #     pos_ext_bboxes = hbb2obb(sampling_result.pos_bboxes)
        # elif hbb_trans == 'hbbpolyobb':
        #     pos_ext_bboxes = hbbpolyobb(sampling_result.pos_bboxes)
        # elif hbb_trans == 'hbb2obb_v2':
        #     pos_ext_bboxes = hbb2obb_v2(sampling_result.pos_bboxes)
        # else:
        #     print('no such hbb2obb trans function')
        #     raise Exception
        pos_ext_bboxes = sampling_result.pos_bboxes # 妈呀这里坑了我好久debug, 要注意
        # print(pos_ext_bboxes.shape)
        if with_module:
            pos_bbox_targets = dbbox2delta(pos_ext_bboxes,
                                           pos_gt_obbs_ts,
                                           target_means, target_stds)
        else:
            pos_bbox_targets = dbbox2delta_v3(pos_ext_bboxes,
                                              pos_gt_obbs_ts,
                                              target_means, target_stds)
        bbox_targets[pos_inds, :] = pos_bbox_targets
        bbox_weights[pos_inds, :] = 1.0 # 对pos进行Bbox回归，而其他的bbox_weight为0,所以不会进行回归
        if gt_labels is None:
            labels[pos_inds] = 1
        else:
            labels[pos_inds] = gt_labels[sampling_result.pos_assigned_gt_inds]
        if cfg.pos_weight <= 0:
            label_weights[pos_inds] = 1.0 # pos在分类时的权重
        else:
            label_weights[pos_inds] = cfg.pos_weight
    if len(neg_inds) > 0:
        label_weights[neg_inds] = 1.0 # neg在分类时的权重，而如果是被忽略的，weight是为0的

    # map up to original set of anchors
    if unmap_outputs:
        num_total_anchors = flat_anchors.size(0)
        labels = unmap(labels, num_total_anchors, inside_flags)
        label_weights = unmap(label_weights, num_total_anchors, inside_flags)
        bbox_targets = unmap(bbox_targets, num_total_anchors, inside_flags)
        bbox_weights = unmap(bbox_weights, num_total_anchors, inside_flags)

    return (labels, label_weights, bbox_targets, bbox_weights, pos_inds,
            neg_inds)


def anchor_inside_flags(flat_anchors, valid_flags, img_shape,
                        allowed_border=0):
    img_h, img_w = img_shape[:2]
    if allowed_border >= 0: # 允许anchor超出图像边界(只能超出allow_border这么多)
        inside_flags = valid_flags & \
            (flat_anchors[:, 0] >= -allowed_border) & \
            (flat_anchors[:, 1] >= -allowed_border) & \
            (flat_anchors[:, 2] < img_w + allowed_border) & \
            (flat_anchors[:, 3] < img_h + allowed_border)
    else:
        inside_flags = valid_flags # 不允许anchor超出图像边界(其实base_anchor还是有可能超出图像大小的)
    return inside_flags


def unmap(data, count, inds, fill=0):
    """ Unmap a subset of item (data) back to the original set of items (of
    size count) """
    if data.dim() == 1:
        ret = data.new_full((count, ), fill)
        ret[inds] = data
    else:
        new_size = (count, ) + data.size()[1:]
        ret = data.new_full(new_size, fill)
        ret[inds, :] = data
    return ret
