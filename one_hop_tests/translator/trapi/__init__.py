"""
Code to submit OneHop tests to TRAPI
"""
from sys import stderr
from typing import Optional, Dict, Set, List
import requests

from reasoner_validator.validator import TRAPIResponseValidator
from reasoner_validator.report import ValidationReporter
from reasoner_validator.trapi import call_trapi, TRAPISchemaValidator

from logging import getLogger

from translator_testing_model.datamodel.pydanticmodel import TestAsset

logger = getLogger()

ARS_HOSTS = [
    'ars-prod.transltr.io',
    'ars.test.transltr.io',
    'ars.ci.transltr.io',
    'ars-dev.transltr.io',
    'ars.transltr.io'
]


def post_query(url: str, query: Dict, params=None, server: str = ""):
    """
    :param url, str URL target for HTTP POST
    :param query, JSON query for posting
    :param params
    :param server, str human-readable name of server called (for error message reports)
    """
    if params is None:
        response = requests.post(url, json=query)
    else:
        response = requests.post(url, json=query, params=params)
    if not response.status_code == 200:
        print(
            f"Server {server} at '\nUrl: '{url}', Query: '{query}' with " +
            f"parameters '{params}' returned HTTP error code: '{response.status_code}'",
            file=stderr
        )
        return {}
    return response.json()


def generate_test_error_msg_prefix(case: Dict, test_name: str) -> str:
    assert case
    test_msg_prefix: str = "test_onehops.py::test_trapi_"
    resource_id: str = ""
    component: str = "kp"
    if 'ara_source' in case and case['ara_source']:
        component = "ara"
        ara_id = case['ara_source'].replace("infores:", "")
        resource_id += ara_id + "|"
    test_msg_prefix += f"{component}s["
    if 'kp_source' in case and case['kp_source']:
        kp_id = case['kp_source'].replace("infores:", "")
        resource_id += kp_id
    edge_idx = case['idx']
    edge_id = generate_edge_id(resource_id, edge_idx)
    if not test_name:
        test_name = "input"
    test_msg_prefix += f"{edge_id}-{test_name}] FAILED"
    return test_msg_prefix


def generate_edge_id(resource_id: str, edge_i: int) -> str:
    return f"{resource_id}#{str(edge_i)}"


class UnitTestReport(ValidationReporter):
    """
    UnitTestReport is a wrapper for ValidationReporter used to aggregate SRI Test actionable validation messages.
    Not to be confused with the translator.sri.testing.report_db.TestReport, which is the comprehensive set
    of all JSON reports from a single SRI Testing harness test run.
    """
    def __init__(self, test_asset: TestAsset, test_name: str):
        ValidationReporter.__init__(
            self,
            prefix=test_name  # TODO: generate_test_error_msg_prefix(test_case, test_name=test_name)
        )
        self.test_asset = test_asset
        self.messages: Dict[str, Set[str]] = {
            "skipped": set(),
            "critical": set(),
            "failed": set(),
            "warning": set(),
            "info": set()
        }
        self.trapi_request: Optional[Dict] = None
        self.trapi_response: Optional[Dict[str, int]] = None

    def get_messages(self) -> Dict[str, List[str]]:
        return {test_name: list(message_set) for test_name, message_set in self.messages.items()}

    def skip(self, code: str, edge_id: str, messages: Optional[Dict] = None):
        """
        Edge test Pytest skipping wrapper.
        :param code: str, validation message code (indexed in the codes.yaml of the Reasoner Validator)
        :param edge_id: str, S-P-O identifier of the edge being skipped
        :param messages: (optional) additional validation messages available to explain why the test is being skipped
        :return:
        """
        self.report(code=code, edge_id=edge_id)
        if messages:
            self.add_messages(messages)
        report_string: str = self.dump_messages(flat=True)
        self.messages["skipped"].add(report_string)

    def assert_test_outcome(self):
        """
        Test outcomes
        """
        if self.has_critical():
            critical_msg = self.dump_critical(flat=True)
            logger.critical(critical_msg)
            self.messages["critical"].add(critical_msg)

        elif self.has_errors():
            # we now treat 'soft' errors similar to critical errors (above) but
            # the validation messages will be differentiated on the user interface
            err_msg = self.dump_errors(flat=True)
            logger.error(err_msg)
            self.messages["failed"].add(err_msg)

        elif self.has_warnings():
            wrn_msg = self.dump_warnings(flat=True)
            logger.warning(wrn_msg)
            self.messages["warning"].add(wrn_msg)

        elif self.has_information():
            info_msg = self.dump_info(flat=True)
            logger.info(info_msg)
            self.messages["info"].add(info_msg)

        else:
            pass  # do nothing... just silent pass through...


def constrain_trapi_request_to_kp(trapi_request: Dict, kp_source: str) -> Dict:
    """
    Method to annotate KP constraint on an ARA call
    as an attribute_constraint object on the test edge.
    :param trapi_request: Dict, original TRAPI message
    :param kp_source: str, KP InfoRes (from kp_source field of test edge)
    :return: Dict, trapi_request annotated with additional KP 'attribute_constraint'
    """
    assert "message" in trapi_request
    message: Dict = trapi_request["message"]
    assert "query_graph" in message
    query_graph: Dict = message["query_graph"]
    assert "edges" in query_graph
    edges: Dict = query_graph["edges"]
    assert "ab" in edges
    edge: Dict = edges["ab"]

    # annotate the edge constraint on the (presumed single) edge object
    edge["attribute_constraints"] = [
        {
            "id": "biolink:knowledge_source",
            "name": "knowledge source",
            "value": [kp_source],
            "operator": "=="
        }
    ]

    return trapi_request


def get_predicate_id(predicate_name: str) -> str:
    """
    SME's (like Jenn) like plain English (possibly capitalized) names
    for their predicates, whereas, we need regular Biolink CURIES here.
    :param predicate_name:
    :return: str, predicate CURIE (presumed to be from the Biolink Model?)
    """
    # TODO: maybe validate the predicate name here against the Biolink Model?
    predicate = predicate_name.lower().replace(" ", "_")
    return f"biolink:{predicate}"


def translate_test_asset(test_asset: TestAsset, biolink_version: str) -> Dict[str, str]:
    """
    Need to access the TestAsset fields as a dictionary with some
    edge attributes relabelled to reasoner-validator expectations.

    :param test_asset: TestAsset received from TestHarness
    :param biolink_version: Biolink Model release assumed for graphs assessed by One Hop testing.
    :return: Dict[str,str], reasoner-validator indexed test edge data.
    """
    test_edge: Dict[str, str] = dict()

    test_edge["idx"] = test_asset.id
    test_edge["subject_id"] = test_asset.input_id
    test_edge["predicate"] = test_asset.predicate_id \
        if test_asset.predicate_id else get_predicate_id(predicate_name=test_asset.predicate_name)
    test_edge["object_id"] = test_asset.output_id
    test_edge["subject_category"] = test_asset.output_id
    test_edge["object_category"] = test_asset.output_id
    test_edge["biolink_version"] = biolink_version

    return test_edge


async def execute_trapi_lookup(
        url: str,
        test_asset: TestAsset,
        creator,
        trapi_version: Optional[str] = None,
        biolink_version: Optional[str] = None,
) -> UnitTestReport:
    """
    Method to execute a TRAPI lookup, using the 'creator' test template.

    :param url: str, target TRAPI url endpoint to be tested
    :param test_asset: TestCase, input data test case
    :param creator: unit test-specific TRAPI query message creator
    :param trapi_version: Optional[str], target TRAPI version
    :param biolink_version: Optional[str], target Biolink Model version
    :return: results: Dict of results
    """
    test_report: UnitTestReport = UnitTestReport(test_asset=test_asset, test_name=creator.__name__)

    trapi_request: Optional[Dict]
    output_element: Optional[str]
    output_node_binding: Optional[str]

    _test_asset = translate_test_asset(test_asset=test_asset, biolink_version=biolink_version)

    trapi_request, output_element, output_node_binding = creator(_test_asset)

    if not trapi_request:
        # output_element and output_node_binding were
        # expropriated by the 'creator' to return error information
        context = output_element.split("|")
        test_report.report(
            code="critical.trapi.request.invalid",
            identifier=context[1],
            context=context[0],
            reason=output_node_binding
        )
    else:

        # sanity check: verify first that the TRAPI request is well-formed by the creator(case)
        validator: TRAPISchemaValidator = TRAPISchemaValidator(trapi_version=trapi_version)
        validator.validate(trapi_request, component="Query")
        test_report.merge(validator)
        if not test_report.has_messages():

            # if no messages are reported, then continue with the validation

            # TODO: this is SRI_Testing harness functionality which we don't yet support here?
            #
            # if 'ara_source' in _test_asset and _test_asset['ara_source']:
            #     # sanity check!
            #     assert 'kp_source' in _test_asset and _test_asset['kp_source']
            #
            #     # Here, we need annotate the TRAPI request query graph to
            #     # constrain an ARA query to the test case specified 'kp_source'
            #     trapi_request = constrain_trapi_request_to_kp(
            #         trapi_request=trapi_request, kp_source=_test_asset['kp_source']
            #     )

            # Make the TRAPI call to the TestCase targeted ARS, KP or
            # ARA resource, using the case-documented input test edge
            trapi_response = await call_trapi(url, trapi_request)

            # Capture the raw TRAPI query input and output
            # for possibly later test harness access
            test_report.trapi_request = trapi_request
            test_report.trapi_response = trapi_response

            # Second sanity check: was the web service (HTTP) call itself successful?
            status_code: int = trapi_response['status_code']
            if status_code != 200:
                test_report.report("critical.trapi.response.unexpected_http_code", identifier=status_code)
            else:
                #########################################################
                # Looks good so far, so now validate the TRAPI response #
                #########################################################
                response: Optional[Dict] = trapi_response['response_json']

                if response:
                    # Report 'trapi_version' and 'biolink_version' recorded
                    # in the 'response_json' (if the tags are provided)
                    if 'schema_version' not in response:
                        test_report.report(code="warning.trapi.response.schema_version.missing")
                    else:
                        trapi_version: str = response['schema_version'] if not trapi_version else trapi_version
                        print(f"execute_trapi_lookup() using TRAPI version: '{trapi_version}'", file=stderr)

                    if 'biolink_version' not in response:
                        test_report.report(code="warning.trapi.response.biolink_version.missing")
                    else:
                        biolink_version = response['biolink_version'] \
                            if not biolink_version else biolink_version
                        logger.info(f"execute_trapi_lookup() using Biolink Model version: '{biolink_version}'")

                    # If nothing badly wrong with the TRAPI Response to this point, then we also check
                    # whether the test input edge was returned in the Response Message knowledge graph
                    #
                    # case: Dict contains something like:
                    #
                    #     idx: 0,
                    #     subject_category: 'biolink:SmallMolecule',
                    #     object_category: 'biolink:Disease',
                    #     predicate: 'biolink:treats',
                    #     subject_id: 'CHEBI:3002',  # may have the deprecated key 'subject' here
                    #     object_id: 'MESH:D001249', # may have the deprecated key 'object' here
                    #
                    # the contents for which ought to be returned in
                    # the TRAPI Knowledge Graph, as a Result mapping?
                    #
                    validator: TRAPIResponseValidator = TRAPIResponseValidator(
                        trapi_version=trapi_version,
                        biolink_version=biolink_version
                    )
                    if not validator.case_input_found_in_response(_test_asset, response, trapi_version):
                        test_edge_id: str = f"{_test_asset['idx']}|" \
                                            f"({_test_asset['subject_id']}#{_test_asset['subject_category']})" + \
                                            f"-[{_test_asset['predicate']}]->" + \
                                            f"({_test_asset['object_id']}#{_test_asset['object_category']})"
                        test_report.report(
                            code="error.trapi.response.knowledge_graph.missing_expected_edge",
                            identifier=test_edge_id
                        )
                else:
                    test_report.report(code="error.trapi.response.empty")

    return test_report


def retrieve_trapi_response(host_url: str, response_id: str):
    try:
        response_content = requests.get(
            f"{host_url}{response_id}",
            headers={'accept': 'application/json'}
        )
        if response_content:
            status_code = response_content.status_code
            if status_code == 200:
                print(f"...Result returned from '{host_url}'!")
        else:
            status_code = 404

    except Exception as e:
        print(f"Remote host {host_url} unavailable: Connection attempt to {host_url} triggered an exception: {e}")
        response_content = None
        status_code = 404

    return status_code, response_content


def retrieve_ars_result(response_id: str, verbose: bool):
    global trapi_response

    if verbose:
        print(f"Trying to retrieve ARS Response UUID '{response_id}'...")

    response_content: Optional = None
    status_code: int = 404

    for ars_host in ARS_HOSTS:
        if verbose:
            print(f"\n...from {ars_host}", end=None)

        status_code, response_content = retrieve_trapi_response(
            host_url=f"https://{ars_host}/ars/api/messages/",
            response_id=response_id
        )
        if status_code != 200:
            continue

    if status_code != 200:
        print(f"Unsuccessful HTTP status code '{status_code}' reported for ARS PK '{response_id}'?")
        return

    # Unpack the response content into a dict
    try:
        response_dict = response_content.json()
    except Exception as e:
        print(f"Cannot decode ARS PK '{response_id}' to a Translator Response, exception: {e}")
        return

    if 'fields' in response_dict:
        if 'actor' in response_dict['fields'] and str(response_dict['fields']['actor']) == '9':
            print("The supplied response id is a collection id. Please supply the UUID for a response")
        elif 'data' in response_dict['fields']:
            print(f"Validating ARS PK '{response_id}' TRAPI Response result...")
            trapi_response = response_dict['fields']['data']
        else:
            print("ARS response dictionary is missing 'fields.data'?")
    else:
        print("ARS response dictionary is missing 'fields'?")
