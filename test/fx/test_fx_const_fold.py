import operator
import unittest

import torch
import torch.fx
from torch.fx.experimental import const_fold


class TestConstFold(unittest.TestCase):
    def _verify_const_fold_mod(self, mod_folded: const_fold.FoldedGraphModule):
        self.assertTrue(mod_folded.const_subgraph_module is not None)

        # Check that the constants are attributes in the main subgraph.
        num_folded_attrs = 0
        for node in mod_folded.graph.nodes:
            if node.op == "get_attr" and (node.target in mod_folded.const_output_names):
                num_folded_attrs += 1
        self.assertEqual(num_folded_attrs, len(mod_folded.const_output_names))

    def test_const_fold_basic_one_attr_no_name_collision(self):
        r"""
        Perform constant folding conversion, from original mod to split constant folding
        module with two split subgraphs, where there's a single attr to fold and
        a single output attr result to replace.

           attr1                 attr1
            | |                   | |
        x   add                   add
         \ /                       |
         sub   y                 output     (becomes attr add_1)
            \ /         ==> -------+------- (const/base subgraph split)
            mul  attr2       x   /          (input from previous subgraph
              \ /             \ /            is attr)
              add             sub   y
               |                 \ /
             output              mul  attr2
                                   \ /
                                   add
                                    |
                                  output
        """

        class ConstFoldTestModule(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.attr_1 = torch.nn.Parameter(torch.tensor([[-0.9]]))
                self.attr_2 = torch.nn.Parameter(torch.tensor([[17.1]]))

            def forward(self, x, y):
                a = self.attr_1 + self.attr_1
                x = x - a
                return x * y + self.attr_2

        mod = ConstFoldTestModule()
        mod_folded: const_fold.FoldedGraphModule = const_fold.split_const_subgraphs(mod)
        self._verify_const_fold_mod(mod_folded)

        # Now run both folded and non-folded to check results equal.
        in_x, in_y = torch.tensor([[-0.45]]), torch.tensor([0.9])
        base_result = mod(in_x, in_y)
        fold_result = mod_folded(in_x, in_y)
        self.assertTrue(torch.equal(fold_result, base_result))

    def test_const_fold_basic_one_attr_name_collision(self):
        r"""
        Perform constant folding conversion, from original mod to split constant folding
        module with two split subgraphs, where there's a single attr to fold and
        a single output attr result to replace. Name the attrs such that they will
        collide by name with folded attrs.

           add_1                 add_1
            | |                   | |
        x   add                   add
         \ /                       |
         sub   y                 output     (becomes attr add_1)
            \ /         ==> -------+------- (const/base subgraph split)
            mul  add_2       x   /          (input from previous subgraph
              \ /             \ /            is attr)
              add             sub   y
               |                 \ /
             output              mul  add_2
                                   \ /
                                   add
                                    |
                                  output
        """

        class ConstFoldTestModule(torch.nn.Module):
            def __init__(self):
                super().__init__()
                # Note: Named as such to result in name collision.
                self.add_1__CF = torch.nn.Parameter(torch.tensor([[1.0]]))
                self.add_2__CF = torch.nn.Parameter(torch.tensor([[17.1]]))

            def forward(self, x, y):
                a = self.add_1__CF + self.add_1__CF
                x = x - a
                return x * y + self.add_2__CF

        mod = ConstFoldTestModule()
        mod_folded: const_fold.FoldedGraphModule = const_fold.split_const_subgraphs(mod)
        self._verify_const_fold_mod(mod_folded)

        # Now run both folded and non-folded to check results equal.
        in_x, in_y = torch.tensor([[5.0]]), torch.tensor([4.0])
        base_result = mod(in_x, in_y)
        fold_result = mod_folded(in_x, in_y)
        self.assertTrue(torch.equal(fold_result, base_result))

    def test_const_fold_basic_placeholder_reordered(self):
        """
        Test code path where placeholder comes after normal op node in FX
        """
        class ConstFoldTestModule(torch.nn.Module):
            def __init__(self):
                super().__init__()

            def forward(self, x, y):
                return x * 2 + y

        mod = ConstFoldTestModule()
        mod = torch.fx.symbolic_trace(mod)
        yy = None
        for n in mod.graph.nodes:
            if n.op == "placeholder" and n.target == "y":
                yy = n
            elif yy is not None and n.op == "call_function":
                yy.prepend(n)
                break

        mod_folded: const_fold.FoldedGraphModule = const_fold.split_const_subgraphs(mod)

        self.assertTrue(mod_folded.const_subgraph_module is None)
        # Now run both folded and non-folded to check results equal.
        in_x = torch.tensor([[-0.45]])
        in_y = torch.tensor([[0.45]])
        base_result = mod(in_x, in_y)
        fold_result = mod_folded(in_x, in_y)
        self.assertTrue(torch.equal(fold_result, base_result))

    def test_const_fold_noop(self):
        r"""
        Check that a graph with no constant folding is handled correctly.

        x  attr1
         \ /
         sub
          |
        output
        """

        class ConstFoldTestModule(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.attr1 = torch.nn.Parameter(torch.tensor([[-0.9]]))

            def forward(self, x):
                return x - self.attr1

        mod = ConstFoldTestModule()
        mod_folded: const_fold.FoldedGraphModule = const_fold.split_const_subgraphs(mod)

        # Check that the folded graph module is None, since there was no folding to do.
        self.assertTrue(mod_folded.const_subgraph_module is None)

        # Now run both folded and non-folded to check results equal.
        in_x = torch.tensor([[-0.45]])
        base_result = mod(in_x)
        fold_result = mod_folded(in_x)
        self.assertTrue(torch.equal(fold_result, base_result))

    def test_const_fold_basic_two_attr_three_input(self):
        r"""
        Perform constant folding conversion, from original mod to split constant
        folding module with two split subgraphs, where there are two attrs to
        fold into a single output, and there are three placeholder inputs.

        attr1   attr2         attr1   attr2
            \   /                 \   /
         x   add                   add
          \ /                       |
          sub     y               output     (becomes attr add_1)
             \   /     ==>   -------+------- (const/base subgraph split)
              mul  z           x   /         (input from previous subgraph
                \ /             \ /           is attr)
                div              sub  y
                 |                 \ /
               output              mul  z
                                     \ /
                                     div
                                      |
                                    output
        """

        class ConstFoldTestModule(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.attr1 = torch.nn.Parameter(torch.tensor([[-0.9]]))
                self.attr1 = torch.nn.Parameter(torch.tensor([[1.32]]))

            def forward(self, x, y, z):
                a = self.attr1 + self.attr1
                sub = x - a
                mul = sub * y
                return mul / z

        mod = ConstFoldTestModule()
        mod_folded: const_fold.FoldedGraphModule = const_fold.split_const_subgraphs(mod)
        self._verify_const_fold_mod(mod_folded)

        # Now run both folded and non-folded to check results equal.
        in_x, in_y, in_z = (
            torch.tensor([[-0.45]]),
            torch.tensor([0.9]),
            torch.tensor([1.1]),
        )
        base_result = mod(in_x, in_y, in_z)
        fold_result = mod_folded(in_x, in_y, in_z)
        self.assertTrue(torch.equal(fold_result, base_result))

    def test_const_fold_basic_two_attr(self):
        r"""
        Perform constant folding conversion, from original mod to split constant
        folding module with two split subgraphs, where there are two attrs to
        fold into a single output.

        attr1  attr2                attr1  attr2
            \ /                         \ /
        x   add                         add       (becomes attr add_1)
         \ /            ==>       -------+------- (const/base subgraph split)
         sub                         x   |        (input from previous subgraph is attr)
          |                           \ /
        output                        sub
                                       |
                                     output
        """

        class ConstFoldTestModule(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.attr1 = torch.nn.Parameter(torch.randn(2, 3))
                self.attr2 = torch.nn.Parameter(torch.randn(2, 3))

            def forward(self, x):
                y = self.attr1 + self.attr2
                return x + y

        mod = ConstFoldTestModule()
        mod_folded: const_fold.FoldedGraphModule = const_fold.split_const_subgraphs(mod)
        self._verify_const_fold_mod(mod_folded)

        # Now run both folded and non-folded to check results equal.
        in_x = torch.randn(2, 3)
        fold_result = mod_folded(in_x)
        base_result = mod(in_x)
        self.assertTrue(torch.equal(fold_result, base_result))

    def test_const_fold_multi_const_folded_attrs(self):
        r"""
        Perform constant folding conversion, from original mod to split constant
        folding module with two split subgraphs, where there are two attrs to
        fold into two new attrs.

           attr1        attr2          attr1     attr2
           /    \         |           /     \      |
        permute  |       sum       permute   |    sum
            \   /        /                \ /      |
         x   add    y   /                 add      |
          \ /        \ /                   |       |
          sub        add                 output  output     (become attrs add_1 and mul_1)
             \       /        ==>   --------+-------+------ (const/base subgraph split)
              \     /                   x   |   y   |       (inputs from previous subgraph
                add                      \ /     \ /         are attrs)
                 |                       sub     add
               linear                       \   /
                 |                           add
               sigmoid                        |
                 |                          linear
               output                         |
                                            sigmoid
                                              |
                                            output
        """

        class ConstFoldTestModule(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.attr1 = torch.nn.Parameter(torch.randn(4, 4))
                self.attr2 = torch.nn.Parameter(torch.randn(4, 4))
                self.lin = torch.nn.Linear(4, 4)

            def forward(self, x, y):
                a = self.attr1 + self.attr1.permute(1, 0)
                x = x - a
                amax = torch.sum(self.attr2, dim=1)
                y = y + amax
                return torch.sigmoid(self.lin(x + y))

        mod = ConstFoldTestModule()
        mod_folded: const_fold.FoldedGraphModule = const_fold.split_const_subgraphs(mod)
        self._verify_const_fold_mod(mod_folded)

        # Now run both folded and non-folded to check results equal.
        in_x, in_y = torch.randn(4, 4), torch.randn(4)
        fold_result = mod_folded(in_x, in_y)
        base_result = mod(in_x, in_y)
        self.assertTrue(torch.equal(fold_result, base_result))

    def test_const_fold_submod_hierarchy(self):
        r"""
        Perform constant folding conversion, from original mod to split constant folding
        module where one of the folded attrs comes from a submod deeper in the hierarchy
        of the base module.
        """

        class TracedThroughModule(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.internal_attr = torch.nn.Parameter(torch.randn(2, 3))

            def forward(self):
                return self.internal_attr

        class ConstFoldTestModule(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.my_mod = TracedThroughModule()
                self.attr = torch.nn.Parameter(torch.randn(2, 3))

            def forward(self, x):
                return self.attr + self.my_mod() + x

        mod = ConstFoldTestModule()
        mod_folded: const_fold.FoldedGraphModule = const_fold.split_const_subgraphs(mod)
        self._verify_const_fold_mod(mod_folded)

        # Now run both folded and non-folded to check results equal.
        in_x = torch.randn(2, 3)
        fold_result = mod_folded(in_x)
        base_result = mod(in_x)
        self.assertTrue(torch.equal(fold_result, base_result))

    def test_retain_node_meta(self):
        r"""
        Perform constant folding conversion, and validate that node meta is retained.
        """

        class ConstFoldTestModule(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.attr = torch.nn.Parameter(torch.randn(2, 3))

            def forward(self, x):
                a = self.attr + self.attr
                return x - a

        mod = ConstFoldTestModule()
        gm = torch.fx.symbolic_trace(mod)

        # Add a count for each node to check after we const fold.
        for idx, node in enumerate(gm.graph.nodes):
            if node.op != "output":
                node.meta["meta_idx"] = idx

        # Pre-folding:
        # idx 0: placeholder
        # idx 1: get_attr (will no longer be used, hence removed)
        # idx 2: add (will be folded into a get_attr)
        # idx 3: sub

        gm_folded: const_fold.FoldedGraphModule = const_fold.split_const_subgraphs(gm)
        self._verify_const_fold_mod(gm_folded)

        # Post-folding:
        # idx 0: placeholder
        # idx 2: get_attr (replaced original add; original get_attr was removed)
        # idx 3: sub

        # Check the expected indices are still here.
        for node in gm_folded.graph.nodes:
            if node.op == "placeholder":
                self.assertEqual(node.meta["meta_idx"], 0)
            elif node.op == "get_attr":
                self.assertEqual(node.meta["meta_idx"], 2)
            elif node.op == "call_function" and node.target == operator.sub:
                self.assertEqual(node.meta["meta_idx"], 3)
            else:
                self.assertEqual(node.op, "output")

        # Now run both folded and non-folded to check results equal.
        in_x = torch.randn(2, 3)
        fold_result = gm_folded(in_x)
        base_result = mod(in_x)
        self.assertTrue(torch.equal(fold_result, base_result))

    def test_const_fold_has_inlined_call_module_node(self):
        class ConstFoldTestModule(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.attr = torch.nn.Parameter(torch.randn(2, 3))
                self.mod = torch.nn.Identity()
                self.mod.relu = torch.nn.ReLU()

            def forward(self, x):
                a = self.attr + self.attr
                return self.mod.relu(x - a)

        mod = ConstFoldTestModule()
        gm_folded = const_fold.split_const_subgraphs(mod)

        # Now run both folded and non-folded to check results equal.
        in_x = torch.randn(2, 3)
        fold_result = gm_folded(in_x)
        base_result = mod(in_x)
        self.assertTrue(torch.equal(fold_result, base_result))
