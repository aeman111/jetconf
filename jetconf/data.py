import json
from threading import Lock
from enum import Enum
from colorlog import error, warning as warn, info, debug
from typing import List, Any, Dict, TypeVar, Tuple, Set
from pydispatch import dispatcher

from yangson.schema import SchemaRoute, SchemaNode, NonexistentSchemaNode, ListNode, LeafListNode
from yangson.context import Context
from yangson.datamodel import InstanceIdentifier, DataModel
from yangson.instance import \
    Instance, \
    NonexistentInstance, \
    InstanceTypeError, \
    DuplicateMember, \
    ArrayValue, \
    ObjectValue, \
    MemberName, \
    EntryKeys, \
    EntryIndex

from .helpers import DataHelpers


class PathFormat(Enum):
    URL = 0
    XPATH = 1


class NacmForbiddenError(Exception):
    def __init__(self, msg="Access to data node rejected by NACM", rule=None):
        self.msg = msg
        self.rulename = rule

    def __str__(self):
        return self.msg


class DataLockError(Exception):
    def __init__(self, msg=""):
        self.msg = msg

    def __str__(self):
        return self.msg


class NoHandlerError(Exception):
    def __init__(self, msg=""):
        self.msg = msg

    def __str__(self):
        return self.msg


class InstanceAlreadyPresent(Exception):
    def __init__(self, msg=""):
        self.msg = msg

    def __str__(self):
        return self.msg


class NoHandlerForOpError(NoHandlerError):
    pass


class NoHandlerForStateDataError(NoHandlerError):
    pass


class BaseDataListener:
    def __init__(self, ds: "BaseDatastore"):
        self._ds = ds
        self.schema_paths = []

    def add_schema_node(self, sch_pth: str):
        sn = self._ds.get_schema_node(sch_pth)
        self.schema_paths.append(sch_pth)
        dispatcher.connect(self.process, str(id(sn)))

    def process(self, sn: SchemaNode, ii: InstanceIdentifier):
        raise NotImplementedError("Not implemented in base class")

    def __str__(self):
        return self.__class__.__name__ + ": listening at " + str(self.schema_paths)


class Rpc:
    def __init__(self):
        self.username = None    # type: str
        self.path = None        # type: str
        self.qs = None          # type: Dict[str, List[str]]
        self.path_format = PathFormat.URL   # type: PathFormat
        self.skip_nacm_check = False        # type: bool
        self.op_name = None                 # type: str
        self.op_input_args = None           # type: ObjectValue


class BaseDatastore:
    def __init__(self, dm: DataModel, name: str=""):
        self.name = name
        self.nacm = None    # type: NacmConfig
        self._data = None   # type: Instance
        self._dm = dm       # type: DataModel
        self._data_lock = Lock()
        self._lock_username = None  # type: str

    # Register NACM module to datastore
    def register_nacm(self, nacm_config: "NacmConfig"):
        self.nacm = nacm_config

    # Returns the root node of data tree
    def get_data_root(self) -> Instance:
        return self._data

    # Get schema node with particular schema address
    def get_schema_node(self, sch_pth: str) -> SchemaNode:
        sn = self._dm.get_schema_node(sch_pth)
        if sn is None:
            raise NonexistentSchemaNode(sch_pth)
        return sn

    # Get schema node for particular data node
    def get_schema_node_ii(self, ii: InstanceIdentifier) -> SchemaNode:
        sn = Context.schema.get_data_descendant(ii)
        return sn

    # Parse Instance Identifier from string
    def parse_ii(self, path: str, path_format: PathFormat) -> InstanceIdentifier:
        if path_format == PathFormat.URL:
            ii = self._dm.parse_resource_id(path)
        else:
            ii = self._dm.parse_instance_id(path)

        return ii

    # Notify data observers about change in datastore
    def notify_edit(self, ii: InstanceIdentifier):
        sn = self.get_schema_node_ii(ii)
        while sn is not None:
            dispatcher.send(str(id(sn)), **{'sn': sn, 'ii': ii})
            sn = sn.parent

    # Just get the node, do not evaluate NACM (for testing purposes)
    def get_node(self, ii: InstanceIdentifier) -> Instance:
        n = self._data.goto(ii)
        return n

    # Just get the node, do not evaluate NACM (for testing purposes)
    def get_node_path(self, path: str, path_format: PathFormat) -> Instance:
        ii = self.parse_ii(path, path_format)
        n = self._data.goto(ii)
        return n

    # Get data node, evaluate NACM if required
    def get_node_rpc(self, rpc: Rpc) -> Instance:
        ii = self.parse_ii(rpc.path, rpc.path_format)
        root = self._data

        sn = self.get_schema_node_ii(ii)
        for state_node_pth in sn.state_roots():
            sn_pth_str = "".join(["/" + pth_seg for pth_seg in state_node_pth])
            # print(sn_pth_str)
            sdh = STATE_DATA_HANDLES.get_handler(sn_pth_str)
            if sdh is not None:
                root = sdh.update_node(ii, root).top()
                self._data = root
            else:
                raise NoHandlerForStateDataError()

        self._data = root
        n = self._data.goto(ii)

        if self.nacm:
            nrpc = self.nacm.get_user_nacm(rpc.username)
            if nrpc.check_data_node_path(ii, Permission.NACM_ACCESS_READ) == Action.DENY:
                raise NacmForbiddenError()
            else:
                # Prun subtree data
                n = nrpc.check_data_read_path(ii)

        return n

    # Create new data node
    def create_node_rpc(self, rpc: Rpc, value: Any, insert=None, point=None):
        # Rest-like version
        # ii = self.parse_ii(rpc.path, rpc.path_format)
        # n = self._data.goto(ii)
        #
        # if self.nacm:
        #     nrpc = self.nacm.get_user_nacm(rpc.username)
        #     if nrpc.check_data_node_path(ii, Permission.NACM_ACCESS_CREATE) == Action.DENY:
        #         raise NacmForbiddenError()
        #
        # if isinstance(n.value, ObjectValue):
        #     # Only one member can be appended at time
        #     value_keys = value.keys()
        #     if len(value_keys) > 1:
        #         raise ValueError("Received data contains more than one object")
        #
        #     recv_object_key = tuple(value_keys)[0]
        #     recv_object_value = value[recv_object_key]
        #
        #     # Check if member is not already present in data
        #     existing_member = None
        #     try:
        #         existing_member = n.member(recv_object_key)
        #     except NonexistentInstance:
        #         pass
        #
        #     if existing_member is not None:
        #         raise DuplicateMember(n, recv_object_key)
        #
        #     # Create new member
        #     new_member_ii = ii + [MemberName(recv_object_key)]
        #     data_doc = DataHelpers.node2doc(new_member_ii, recv_object_value)
        #     data_doc_inst = self._dm.from_raw(data_doc)
        #     new_value = data_doc_inst.goto(new_member_ii).value
        #
        #     new_n = n.new_member(recv_object_key, new_value)
        #     self._data = new_n.top()
        # elif isinstance(n.value, ArrayValue):
        #     # Append received node to list
        #     data_doc = DataHelpers.node2doc(ii, [value])
        #     print(data_doc)
        #     data_doc_inst = self._dm.from_raw(data_doc)
        #     new_value = data_doc_inst.goto(ii).value
        #
        #     if insert == "first":
        #         new_n = n.update(ArrayValue(val=new_value + n.value))
        #     else:
        #         new_n = n.update(ArrayValue(val=n.value + new_value))
        #     self._data = new_n.top()
        # else:
        #     raise InstanceTypeError(n, "Child node can only be appended to Object or Array")
        #
        # self.notify_edit(ii)

        # Restconf draft compliant version
        ii = self.parse_ii(rpc.path, rpc.path_format)
        n = self._data.goto(ii)
        new_n = n

        if self.nacm:
            nrpc = self.nacm.get_user_nacm(rpc.username)
            if nrpc.check_data_node_path(ii, Permission.NACM_ACCESS_CREATE) == Action.DENY:
                raise NacmForbiddenError()

        input_member_name = tuple(value.keys())
        if len(input_member_name) != 1:
            raise ValueError("Received json object must contain exactly one member")
        else:
            input_member_name = input_member_name[0]

        input_member_value = value[input_member_name]

        existing_member = None
        try:
            existing_member = n.member(input_member_name)
        except NonexistentInstance:
            pass

        if existing_member is None:
            # Create new data node

            # Convert input data from List/Dict to ArrayValue/ObjectValue
            data_doc = DataHelpers.node2doc(ii + [MemberName(input_member_name)], input_member_value)
            data_doc_inst = self._dm.from_raw(data_doc)
            new_value = data_doc_inst.goto(ii).value
            new_value_data = new_value[input_member_name]

            # Create new node (object member)
            new_n = n.new_member(input_member_name, new_value_data)
        elif isinstance(existing_member.value, ArrayValue):
            # Append received node to list

            # Convert input data from List/Dict to ArrayValue/ObjectValue
            data_doc = DataHelpers.node2doc(ii + [MemberName(input_member_name)], [input_member_value])
            data_doc_inst = self._dm.from_raw(data_doc)
            new_value = data_doc_inst.goto(ii).value
            new_value_data = new_value[input_member_name][0]

            # Get schema node
            sn = self.get_schema_node_ii(ii + [MemberName(input_member_name)])

            if isinstance(sn, ListNode):
                list_node_key = sn.keys[0][0]
                if new_value_data[list_node_key] in map(lambda x: x[list_node_key], existing_member.value):
                    raise InstanceAlreadyPresent("Duplicate key")

                if insert == "first":
                    new_n = existing_member.update(ArrayValue(val=[new_value_data] + existing_member.value))
                elif (insert == "last") or insert is None:
                    new_n = existing_member.update(ArrayValue(val=existing_member.value + [new_value_data]))
                elif insert == "before":
                    entry_sel = EntryKeys({list_node_key: point})
                    list_entry = entry_sel.goto_step(existing_member)
                    new_n = list_entry.insert_before(new_value_data).up()
                elif insert == "after":
                    entry_sel = EntryKeys({list_node_key: point})
                    list_entry = entry_sel.goto_step(existing_member)
                    new_n = list_entry.insert_after(new_value_data).up()
            elif isinstance(sn, LeafListNode):
                if insert == "first":
                    new_n = existing_member.update(ArrayValue(val=[new_value_data] + existing_member.value))
                elif (insert == "last") or insert is None:
                    new_n = existing_member.update(ArrayValue(val=existing_member.value + [new_value_data]))
            else:
                raise InstanceTypeError(n, "Target node must be List or LeafList")

        else:
            raise InstanceAlreadyPresent()

        self._data = new_n.top()
        self.notify_edit(ii)

    # Update already existing data node
    def update_node_rpc(self, rpc: Rpc, value: Any):
        ii = self.parse_ii(rpc.path, rpc.path_format)
        n = self._data.goto(ii)

        if self.nacm:
            nrpc = self.nacm.get_user_nacm(rpc.username)
            if nrpc.check_data_node_path(ii, Permission.NACM_ACCESS_UPDATE) == Action.DENY:
                raise NacmForbiddenError()

        data_doc = DataHelpers.node2doc(ii, value)
        data_doc_inst = self._dm.from_raw(data_doc)
        new_value = data_doc_inst.goto(ii).value

        new_n = n.update(new_value)
        self._data = new_n.top()

        self.notify_edit(ii)

    # Delete data node
    def delete_node_rpc(self, rpc: Rpc, insert=None, point=None):
        ii = self.parse_ii(rpc.path, rpc.path_format)
        n = self._data.goto(ii)
        n_parent = n.up()
        new_n = n_parent
        last_isel = ii[-1]

        if self.nacm:
            nrpc = self.nacm.get_user_nacm(rpc.username)
            if nrpc.check_data_node_path(ii, Permission.NACM_ACCESS_DELETE) == Action.DENY:
                raise NacmForbiddenError()

        if isinstance(n_parent.value, ArrayValue):
            if isinstance(last_isel, EntryIndex):
                new_n = n_parent.remove_entry(last_isel.index)
            elif isinstance(last_isel, EntryKeys):
                new_n = n_parent.remove_entry(n.crumb.pointer_fragment())
        elif isinstance(n_parent.value, ObjectValue):
            if isinstance(last_isel, MemberName):
                new_n = n_parent.remove_member(last_isel.name)
        else:
            raise InstanceTypeError(n, "Invalid target node type")

        self._data = new_n.top()

    # Invoke an operation
    def invoke_op_rpc(self, rpc: Rpc) -> ObjectValue:
        if self.nacm and (not rpc.skip_nacm_check):
            nrpc = self.nacm.get_user_nacm(rpc.username)
            if nrpc.check_rpc_name(rpc.op_name) == Action.DENY:
                raise NacmForbiddenError("Op \"{}\" invocation denied for user \"{}\"".format(rpc.op_name, rpc.username))

        op_handler = OP_HANDLERS.get_handler(rpc.op_name)
        if op_handler is None:
            raise NoHandlerForOpError()

        # Print operation input schema
        # sn = self.get_schema_node(rpc.path)
        # sn_input = sn.get_child("input")
        # if sn_input is not None:
        #     print("RPC input schema:")
        #     print(sn_input._ascii_tree(""))

        ret_data = op_handler(rpc.op_input_args)
        return ret_data

    # Locks datastore data
    def lock_data(self, username: str = None, blocking: bool=True):
        ret = self._data_lock.acquire(blocking=blocking, timeout=1)
        if ret:
            self._lock_username = username or "(unknown)"
            debug("Acquired lock in datastore \"{}\" for user \"{}\"".format(self.name, username))
        else:
            raise DataLockError(
                "Failed to acquire lock in datastore \"{}\" for user \"{}\", already locked by \"{}\"".format(
                    self.name,
                    username,
                    self._lock_username
                )
            )

    # Unlock datastore data
    def unlock_data(self):
        self._data_lock.release()
        debug("Released lock in datastore \"{}\" for user \"{}\"".format(self.name, self._lock_username))
        self._lock_username = None

    # Load data from persistent storage
    def load(self, filename: str):
        raise NotImplementedError("Not implemented in base class")

    # Save data to persistent storage
    def save(self, filename: str):
        raise NotImplementedError("Not implemented in base class")


class JsonDatastore(BaseDatastore):
    def load(self, filename: str):
        self._data = None
        with open(filename, "rt") as fp:
            self._data = self._dm.from_raw(json.load(fp))

    def save(self, filename: str):
        with open(filename, "w") as jfd:
            self.lock_data("json_save")
            json.dump(self._data, jfd)
            self.unlock_data()


def test():
    datamodel = DataHelpers.load_data_model("./data", "./data/yang-library-data.json")
    data = JsonDatastore(datamodel)
    data.load("jetconf/example-data.json")

    rpc = Rpc()
    rpc.username = "dominik"
    rpc.path = "/dns-server:dns-server/zones/zone[domain='example.com']/query-module"
    rpc.path_format = PathFormat.XPATH

    info("Reading: " + rpc.path)
    n = data.get_node_rpc(rpc)
    info("Result =")
    print(n.value)
    expected_value = \
        [
            {'name': 'test1', 'type': 'knot-dns:synth-record'},
            {'name': 'test2', 'type': 'knot-dns:synth-record'}
        ]

    if json.loads(json.dumps(n.value)) == expected_value:
        info("OK")
    else:
        warn("FAILED")

from .nacm import NacmConfig, Permission, Action
from .handler_list import OP_HANDLERS, STATE_DATA_HANDLES
