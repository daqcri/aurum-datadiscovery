import itertools

from modelstore.elasticstore import KWType

from api.apiutils import compute_field_id as id_from
from api.apiutils import Operation
from api.apiutils import OP
from api.apiutils import Relation
from api.apiutils import DRS
from api.apiutils import DRSMode
from api.apiutils import Hit
from api.annotation import MDClass
from api.annotation import MDRelation
from api.annotation import MDHit
from api.annotation import MDComment
from api.annotation import MRS


class Algebra:

    def __init__(self, network, store_client):
        self._network = network
        self._store_client = store_client

    """
    Metadata API
    """
    def annotate(self, author: str, text: str, md_class: MDClass,
        general_source, ref={"general_target": None, "type": None}) -> MRS:
        """
        Create a new annotation in the elasticsearch graph.
        :param author: identifiable name of user or process
        :param text: free text description
        :param md_class: MDClass
        :param general_source: nid, node tuple, Hit, or DRS
        :param ref: (optional) {
            "general_target": nid, node tuple, Hit, or DRS,
            "type": MDRelation
        }
        :return: MRS of the new metadata
        """
        source = self._general_to_drs(general_source)
        target = self._general_to_drs(ref["general_target"])

        if source.mode != DRSMode.FIELDS or target.mode != DRSMode.FIELDS:
            raise ValueError("source and targets must be columns")

        md_class = self._mdclass_to_str(md_class)
        md_hits = []

        # non-relational metadata
        if ref["type"] is None:
            for hit_source in source:
                res = self._store_client.add_annotation(
                    author=author,
                    text=text,
                    md_class=md_class,
                    source=hit_source.nid)
                md_hits.append(res)
            return MRS(md_hits)

        # relational metadata
        md_relation, nid_is_source = self._mdrelation_to_str(ref["type"])
        if not nid_is_source:
            source, target = target, source

        for hit_source in source:
            for hit_target in target:
                res = self._store_client.add_annotation(
                    author=author,
                    text=text,
                    md_class=md_class,
                    source=hit_source.nid,
                    target={"id": hit_target.nid, "type": md_relation})
                md_hits.append(res)
            return MRS(md_hits)

    def add_comments(self, author: str, comments: list, md_id: str) -> MRS:
        """
        Add comments to the annotation with the given md_id.
        :param author: identifiable name of user or process
        :param comments: list of free text comments
        :param md_id: metadata id
        """
        md_comments = []
        for comment in comments:
            res = self._store_client.add_comment(
                author=author, text=comment, md_id=md_id)
            md_comments.append(res)
        return MRS(md_comments)

    def add_tags(self, author: str, tags: list, md_id: str):
        """
        Add tags/keywords to metadata with the given md_id.
        :param md_id: metadata id
        :param tags: a list of tags to add
        """
        return self._store_client.add_tags(author, tags, md_id)

    def md_search(self, general_input=None,
                        relation: MDRelation=None) -> MRS:
        """
        Searches for metadata that reference the nodes in the general
        input. If a relation is given, searches for metadata that mention the
        nodes as the source of the relation. If no parameters are given,
        searches for all metadata.
        :param general_input: nid, node tuple, Hit, or DRS
        :param relation: an MDRelation
        """
        # return all metadata
        if general_input is None:
            return MRS([x for x in self._store_client.get_metadata()])

        drs_nodes = self._general_to_drs(general_input)
        if drs_nodes.mode != DRSMode.FIELDS:
            raise ValueError("general_input must be columns")

        # return metadata that reference the input
        if relation is None:
            md_hits = []
            for node in drs_nodes:
                md_hits.extend(self._store_client.get_metadata(nid=node.nid))
            return MRS(md_hits)

        # return metadata that reference the input with the given relation
        md_hits = []
        store_relation, nid_is_source = self._mdrelation_to_str(relation)
        for node in drs_nodes:
            md_hits.extend(self._store_client.get_metadata(nid=node.nid,
                relation=store_relation, nid_is_source=nid_is_source))
        return MRS(md_hits)

    def md_keyword_search(self, kw: str, max_results=10) -> MRS:
        """
        Performs a keyword search over metadata annotations and comments.
        :param kw: the keyword to search
        :param max_results: maximum number of results to return
        :return: returns a MRS
        """
        hits = self._store_client.search_keywords_md(
            keywords=kw, max_hits=max_results)

        mrs = MRS([x for x in hits])
        return mrs

    """
    Basic API
    """

    def keyword_search(self, kw: str, kw_type: KWType, max_results=10) -> DRS:
        """
        Performs a keyword search over the contents of the data.
        Scope specifies where elasticsearch should be looking for matches.
        i.e. table titles (SOURCE), columns (FIELD), or comment (SOURCE)

        :param kw: the keyword to serch
        :param max_results: maximum number of results to return
        :return: returns a DRS
        """

        hits = self._store_client.search_keywords(
            keywords=kw, elasticfieldname=kw_type, max_hits=max_results)

        # materialize generator
        drs = DRS([x for x in hits], Operation(OP.KW_LOOKUP, params=[kw]))
        return drs

    def neighbor_search(self,
                        general_input,
                        relation: Relation,
                        max_hops=None):
        """
        Given an nid, node, hit or DRS, finds neighbors with specified
        relation.
        :param nid, node tuple, Hit, or DRS:
        """
        # convert whatever input to a DRS
        i_drs = self._general_to_drs(general_input)

        # prepare an output DRS
        o_drs = DRS([], Operation(OP.NONE))
        o_drs = o_drs.absorb_provenance(i_drs)

        # get all of the table Hits in a DRS, if necessary.
        if i_drs.mode == DRSMode.TABLE:
            self._general_to_field_drs(i_drs)

        # Check neighbors
        for h in i_drs:
            hits_drs = self._network.neighbors_id(h, relation)
            o_drs = o_drs.absorb(hits_drs)
        return o_drs

    """
    TC API
    """

    def paths(self, drs_a: DRS, drs_b: DRS, relation=Relation.PKFK, max_hops=2) -> DRS:
        """
        Is there a transitive relationship between any element in a with any
        element in b?
        This function finds the answer constrained on the primitive
        (singular for now) that is passed as a parameter.
        If b is not passed, assumes the user is searching for paths between
        elements in a.
        :param a: DRS
        :param b: DRS
        :param Relation: Relation
        :return:
        """
        # create b if it wasn't passed in.
        drs_a = self._general_to_drs(drs_a)
        drs_b = self._general_to_drs(drs_b)

        self._assert_same_mode(drs_a, drs_b)

        # absorb the provenance of both a and b
        o_drs = DRS([], Operation(OP.NONE))
        o_drs.absorb_provenance(drs_a)
        if drs_b != drs_a:
            o_drs.absorb_provenance(drs_b)

        for h1, h2 in itertools.product(drs_a, drs_b):

            # there are different network operations for table and field mode
            res_drs = None
            if drs_a.mode == DRSMode.FIELDS:
                res_drs = self._network.find_path_hit(
                    h1, h2, relation, max_hops=max_hops)
            else:
                res_drs = self._network.find_path_table(
                    h1, h2, relation, self, max_hops=max_hops)

            o_drs = o_drs.absorb(res_drs)

        return o_drs

    def traverse(self, a: DRS, primitive, max_hops=2) -> DRS:
        """
        Conduct a breadth first search of nodes matching a primitive, starting
        with an initial DRS.
        :param a: a nid, node, tuple, or DRS
        :param primitive: The element to search
        :max_hops: maximum number of rounds on the graph
        """
        a = self._general_to_drs(a)

        o_drs = DRS([], Operation(OP.NONE))

        if a.mode == DRSMode.TABLE:
            raise ValueError(
                'input mode DRSMode.TABLE not supported')

        fringe = a
        o_drs.absorb_provenance(a)
        while max_hops > 0:
            max_hops = max_hops - 1
            for h in fringe:
                hits_drs = self.__network.neighbors_id(h, primitive)
                o_drs = self.union(o_drs, hits_drs)
            fringe = o_drs  # grow the initial input
        return o_drs

    """
    Combiner API
    """

    def intersection(self, a: DRS, b: DRS) -> DRS:
        """
        Returns elements that are both in a and b
        :param a: an iterable object
        :param b: another iterable object
        :return: the intersection of the two provided iterable objects
        """
        a = self._general_to_drs(a)
        b = self._general_to_drs(b)
        self._assert_same_mode(a, b)

        o_drs = a.intersection(b)
        return o_drs

    def union(self, a: DRS, b: DRS) -> DRS:
        """
        Returns elements that are in either a or b
        :param a: an iterable object
        :param b: another iterable object
        :return: the union of the two provided iterable objects
        """
        a = self._general_to_drs(a)
        b = self._general_to_drs(b)
        self._assert_same_mode(a, b)

        o_drs = a.union(b)
        return o_drs

    def difference(self, a: DRS, b: DRS) -> DRS:
        a = self._general_to_drs(a)
        b = self._general_to_drs(b)
        """
        Returns elements that are in either a or b
        :param a: an iterable object
        :param b: another iterable object
        :return: the union of the two provided iterable objects
        """
        a = self._general_to_drs(a)
        b = self._general_to_drs(b)
        self._assert_same_mode(a, b)

        o_drs = a.set_difference(b)
        return o_drs

    """
    Helper Functions
    """

    def make_drs(self, general_input):
        '''
        Makes a DRS from general_input.
        general_input can include an array of strings, Hits, DRS's, etc,
        or just a single DRS.
        '''
        try:

            # If this is a list of inputs, condense it into a single drs
            if isinstance(general_input, list):
                general_input = [
                    self._general_to_drs(x) for x in general_input]

                combined_drs = DRS([], Operation(OP.NONE))
                for drs in general_input:
                    combined_drs = self.union(combined_drs, drs)
                general_input = combined_drs

            # else, just convert it to a DRS
            o_drs = self._general_to_drs(general_input)
            return o_drs
        except:
            msg = (
                '--- Error ---' +
                '\nThis function returns domain result set from the ' +
                'supplied input' +
                '\nusage:\n\tmake_drs( table name/hit id | [table name/hit ' +
                'id, drs/hit/string/int] )' +
                '\ne.g.:\n\tmake_drs(1600820766)')
            print(msg)

    def _general_to_drs(self, general_input) -> DRS:
        """
        Given an nid, node, hit, or DRS and convert it to a DRS.
        :param nid: int
        :param node: (db_name, source_name, field_name)
        :param hit: Hit
        :param DRS: DRS
        :return: DRS
        """
        # test for DRS initially for speed
        if isinstance(general_input, DRS):
            return general_input

        if general_input is None:
            general_input = DRS(data=[], operation=Operation(OP.NONE))

        # Test for ints or strings that represent integers
        if self._represents_int(general_input):
            general_input = self._nid_to_hit(general_input)

        # Test for strings that represent tables
        if isinstance(general_input, str):
            hits = self._network.get_hits_from_table(general_input)
            general_input = DRS([x for x in hits], Operation(OP.ORIGIN))

        # Test for tuples that are not Hits
        if (isinstance(general_input, tuple) and
                not isinstance(general_input, Hit)):
            general_input = self._node_to_hit(general_input)

        # Test for Hits
        if isinstance(general_input, Hit):
            field = general_input.field_name
            if field is '' or field is None:
                # If the Hit's field is not defined, it is in table mode
                # and all Hits from the table need to be found
                general_input = self._hit_to_drs(
                    general_input, table_mode=True)
            else:
                general_input = self._hit_to_drs(general_input)
        if isinstance(general_input, DRS):
            return general_input

        raise ValueError(
            'Input is not None, an integer, field tuple, Hit, or DRS')

    def _nid_to_hit(self, nid: int) -> Hit:
        """
        Given a node id, convert it to a Hit
        :param nid: int or string
        :return: DRS
        """
        nid = str(nid)
        score = 0.0
        nid, db, source, field = self._network.get_info_for([nid])[0]
        hit = Hit(nid, db, source, field, score)
        return hit

    def _node_to_hit(self, node: (str, str, str)) -> Hit:
        """
        Given a field and source name, it returns a Hit with its representation
        :param node: a tuple with the name of the field,
            (db_name, source_name, field_name)
        :return: Hit
        """
        db, source, field = node
        nid = id_from(db, source, field)
        hit = Hit(nid, db, source, field, 0)
        return hit

    def _hit_to_drs(self, hit: Hit, table_mode=False) -> DRS:
        """
        Given a Hit, return a DRS. If in table mode, the resulting DRS will
        contain Hits representing that table.
        :param hit: Hit
        :param table_mode: if the Hit represents an entire table
        :return: DRS
        """
        drs = None
        if table_mode:
            table = hit.source_name
            hits = self._network.get_hits_from_table(table)
            drs = DRS([x for x in hits], Operation(OP.TABLE, params=[hit]))
            drs.set_table_mode()
        else:
            drs = DRS([hit], Operation(OP.ORIGIN))

        return drs

    def _general_to_field_drs(self, general_input):
        drs = self._general_to_drs(general_input)

        drs.set_fields_mode()
        for h in drs:
            fields_table = self._hit_to_drs(h, table_mode=True)
            drs = drs.absorb(fields_table)

        return drs

    def _mdclass_to_str(self, md_class: MDClass):
        ref_table = {
            MDClass.WARNING: "warning",
            MDClass.INSIGHT: "insight",
            MDClass.QUESTION: "question"
        }
        return ref_table[md_class]

    def _mdrelation_to_str(self, md_relation: MDRelation):
        """
        :return: (str, nid_is_source)
        """
        ref_table = {
            MDRelation.MEANS_SAME_AS: ("same", True),
            MDRelation.MEANS_DIFF_FROM: ("different", True),
            MDRelation.IS_SUBCLASS_OF: ("subclass", True),
            MDRelation.IS_SUPERCLASS_OF: ("subclass", False),
            MDRelation.IS_MEMBER_OF: ("member", True),
            MDRelation.IS_CONTAINER_OF: ("member", False)
        }
        return ref_table[md_relation]

    def _assert_same_mode(self, a: DRS, b: DRS) -> None:
        error_text = ("Input parameters are not in the same mode ",
                      "(fields, table)")
        assert a.mode == b.mode, error_text

    def _represents_int(self, string: str) -> bool:
        try:
            int(string)
            return True
        except:
            return False


class API(Algebra):
    def __init__(self, *args, **kwargs):
        super(API, self).__init__(*args, **kwargs)


if __name__ == '__main__':
    print("Aurum API")
