# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import os
from collections import namedtuple, defaultdict

from wptmanifest.node import (DataNode, ConditionalNode, BinaryExpressionNode,
                              BinaryOperatorNode, VariableNode, StringNode, NumberNode,
                              UnaryExpressionNode, UnaryOperatorNode, KeyValueNode)
from wptmanifest.backends import conditional
from wptmanifest.backends.conditional import ManifestItem

import expected

Result = namedtuple("Result", ["run_info", "status"])


def data_cls_getter(output_node, visited_node):
    # visited_node is intentionally unused
    if output_node is None:
        return ExpectedManifest
    elif isinstance(output_node, ExpectedManifest):
        return TestNode
    elif isinstance(output_node, TestNode):
        return SubtestNode
    else:
        raise ValueError


class ExpectedManifest(ManifestItem):
    def __init__(self, node, test_path=None):
        if node is None:
            node = DataNode(None)
        ManifestItem.__init__(self, node)
        self.child_map = {}
        self.test_path = test_path
        self.modified = False

    def append(self, child):
        ManifestItem.append(self, child)
        self.child_map[child.id] = child
        assert len(self.child_map) == len(self.children)

    def _remove_child(self, child):
        del self.child_map[child.id]
        ManifestItem._remove_child(self, child)
        assert len(self.child_map) == len(self.children)

    def get_test(self, test_id):
        return self.child_map[test_id]

    def has_test(self, test_id):
        return test_id in self.child_map


class TestNode(ManifestItem):
    def __init__(self, node):
        ManifestItem.__init__(self, node)
        self.updated_expected = []
        self.new_expected = []
        self.subtests = {}
        self.default_status = None
        self._from_file = True

    @classmethod
    def create(cls, test_type, test_id):
        if test_type == "reftest":
            url = test_id[0]
        else:
            url = test_id
        name = url.split("/")[-1]
        node = DataNode(name)
        self = cls(node)

        self.set("type", test_type)
        if test_type == "reftest":
            self.set("reftype", test_id[1])
            self.set("refurl", test_id[2])
        self._from_file = False
        return self

    @property
    def is_empty(self):
        required_keys = set(["type"])
        if self.test_type == "reftest":
            required_keys |= set(["reftype", "refurl"])
        if set(self._data.keys()) != required_keys:
            return False
        return all(child.is_empty for child in self.children)

    @property
    def test_type(self):
        return self.get("type", None)

    @property
    def id(self):
        components = self.parent.test_path.split(os.path.sep)[:-1]
        components.append(self.name)
        url = "/" + "/".join(components)
        if self.test_type == "reftest":
            return (url, self.get("reftype", None), self.get("refurl", None))
        else:
            return url

    def disabled(self, run_info):
        return self.get("disabled", run_info) is not None

    def set_result(self, run_info, result):
        found = False

        if self.default_status is not None:
            assert self.default_status == result.default_expected
        else:
            self.default_status = result.default_expected

        # Add this result to the list of results satifying
        # any condition in the list of updated results it matches
        for (cond, values) in self.updated_expected:
            if cond(run_info):
                values.append(Result(run_info, result.status))
                if result.status != cond.value:
                    self.root.modified = True
                found = True
                break

        # We didn't find a previous value for this
        if not found:
            self.new_expected.append(Result(run_info, result.status))
            self.root.modified = True

    def coalesce_expected(self):
        final_conditionals = []

        try:
            unconditional_status = self.get("expected")
        except KeyError:
            unconditional_status = self.default_status

        for conditional_value, results in self.updated_expected:
            if not results:
                # The conditional didn't match anything in these runs so leave it alone
                final_conditionals.append(conditional_value)
            elif all(results[0].status == result.status for result in results):
                # All the new values for this conditional matched, so update the node
                result = results[0]
                if (result.status == unconditional_status and
                    conditional_value.condition_node is not None):
                    self.remove_value("expected", conditional_value)
                else:
                    conditional_value.value = result.status
                    final_conditionals.append(conditional_value)
            elif conditional_value.condition_node is not None:
                # Blow away the existing condition and rebuild from scratch
                # This isn't sure to work if we have a conditional later that matches
                # these values too, but we can hope, verify that we get the results
                # we expect, and if not let a human sort it out
                self.remove_value("expected", conditional_value)
                self.new_expected.extend(results)
            elif conditional_value.condition_node is None:
                self.new_expected.extend(result for result in results
                                         if result.status != unconditional_status)

        # It is an invariant that nothing in new_expected matches an existing
        # condition except for the default condition

        if self.new_expected:
            if all(self.new_expected[0].status == result.status
                   for result in self.new_expected) and not self.updated_expected:
                status = self.new_expected[0].status
                if status != self.default_status:
                    self.set("expected", status, condition=None)
                    final_conditionals.append(self._data["expected"][-1])
            else:
                for conditional_node, status in group_conditionals(self.new_expected):
                    if status != unconditional_status:
                        self.set("expected", status, condition=conditional_node.children[0])
                        final_conditionals.append(self._data["expected"][-1])

        if ("expected" in self._data and
            len(self._data["expected"]) > 0 and
            self._data["expected"][-1].condition_node is None and
            self._data["expected"][-1].value == self.default_status):

            self.remove_value("expected", self._data["expected"][-1])

        if ("expected" in self._data and
            len(self._data["expected"]) == 0):
            for child in self.node.children:
                if (isinstance(child, KeyValueNode) and
                    child.data == "expected"):
                    child.remove()
                    break

    def _add_key_value(self, node, values):
        ManifestItem._add_key_value(self, node, values)
        if node.data == "expected":
            self.updated_expected = []
            for value in values:
                self.updated_expected.append((value, []))

    def clear_expected(self):
        self.updated_expected = []
        if "expected" in self._data:
            for child in self.node.children:
                if (isinstance(child, KeyValueNode) and
                    child.data == "expected"):
                    child.remove()
                    del self._data["expected"]
                    break

        for subtest in self.subtests.itervalues():
            subtest.clear_expected()

    def append(self, node):
        child = ManifestItem.append(self, node)
        self.subtests[child.name] = child

    def get_subtest(self, name):
        if name in self.subtests:
            return self.subtests[name]
        else:
            subtest = SubtestNode.create(name)
            self.append(subtest)
            return subtest


class SubtestNode(TestNode):
    def __init__(self, node):
        assert isinstance(node, DataNode)
        TestNode.__init__(self, node)

    @classmethod
    def create(cls, name):
        node = DataNode(name)
        self = cls(node)
        return self

    @property
    def is_empty(self):
        if self._data:
            return False
        return True


def group_conditionals(values):
    by_property = defaultdict(set)
    for result in values:
        run_info, status = result
        for prop_name, prop_value in run_info.iteritems():
            by_property[(prop_name, prop_value)].add(status)

    # If we have more than one value, remove any properties that are common
    # for all the values
    if len(values) > 1:
        for key, statuses in by_property.copy().iteritems():
            if len(statuses) == len(values):
                del by_property[key]

    properties = set(item[0] for item in by_property.iterkeys())

    prop_order = ["debug", "os", "version", "processor", "bits"]
    include_props = []

    for prop in prop_order:
        if prop in properties:
            include_props.append(prop)

    conditions = {}

    for result in values:
        run_info, status = result
        prop_set = tuple((prop, run_info[prop]) for prop in include_props)
        if prop_set in conditions:
            continue

        expr = make_expr(prop_set, status)
        conditions[prop_set] = (expr, status)

    return conditions.values()


def make_expr(prop_set, status):
    """Create an AST that returns the value ``status`` given all the
    properties in prop_set match."""
    root = ConditionalNode()

    assert len(prop_set) > 0

    no_value_props = set(["debug"])

    expressions = []
    for prop, value in prop_set:
        number_types = (int, float, long)
        value_cls = (NumberNode
                     if type(value) in number_types
                     else StringNode)
        if prop not in no_value_props:
            expressions.append(
                BinaryExpressionNode(
                    BinaryOperatorNode("=="),
                    VariableNode(prop),
                    value_cls(unicode(value))
                ))
        else:
            if value:
                expressions.append(VariableNode(prop))
            else:
                expressions.append(
                    UnaryExpressionNode(
                        UnaryOperatorNode("not"),
                        VariableNode(prop)
                    ))
    if len(expressions) > 1:
        prev = expressions[-1]
        for curr in reversed(expressions[:-1]):
            node = BinaryExpressionNode(
                BinaryOperatorNode("and"),
                curr,
                prev)
            prev = node
    else:
        node = expressions[0]

    root.append(node)
    root.append(StringNode(status))

    return root


def get_manifest(metadata_root, test_path):
    manifest_path = expected.expected_path(metadata_root, test_path)
    try:
        with open(manifest_path) as f:
            return compile(f, test_path)
    except IOError:
        return None


def compile(manifest_file, test_path):
    return conditional.compile(manifest_file,
                               data_cls_getter=data_cls_getter,
                               test_path=test_path)
