import torch

from ..base import ReplacePatternBase, MatcherNode


class ReplacePattern(ReplacePatternBase):
    """去掉网络中的identity算子"""

    def __init__(self):
        super(ReplacePattern, self).__init__()

    def make_ops(self):
        """匹配identity op"""
        return [
            MatcherNode("identity", inputs=[None], op_type=[torch.nn.Identity]),
        ]

    def get_new_graph(self, nodes_dict, modules_dict, model=None, transform_idx=None):
        """移除identity op"""
        identity_node = nodes_dict["identity"]
        return identity_node.args[0]
