import dataclasses
import os
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, List, Optional, Tuple, Type, cast

import click
import fmf
import pkg_resources

import tmt
import tmt.steps
from tmt.steps import Action
from tmt.steps.provision import Guest
from tmt.utils import GeneralError

if TYPE_CHECKING:
    import tmt.options
    import tmt.steps.discover
    import tmt.steps.provision

# Test data directory name
TEST_DATA = 'data'

# Default test framework
DEFAULT_FRAMEWORK = 'shell'

# The main test output filename
TEST_OUTPUT_FILENAME = 'output.txt'

# Scripts source directory
SCRIPTS_SRC_DIR = pkg_resources.resource_filename(
    'tmt', 'steps/execute/scripts')


@dataclass
class Script:
    """ Represents a script provided by the internal executor """
    path: str
    aliases: List[str]
    related_variables: List[str]


@dataclass
class ScriptCreatingFile(Script):
    """ Represents a script which creates a file """
    created_file: str


# Script handling reboots, in restraint compatible fashion
TMT_REBOOT_SCRIPT = ScriptCreatingFile(
    path="/usr/local/bin/tmt-reboot",
    aliases=[
        "/usr/local/bin/rstrnt-reboot",
        "/usr/local/bin/rhts-reboot"],
    related_variables=[
        "TMT_REBOOT_COUNT",
        "REBOOTCOUNT",
        "RSTRNT_REBOOTCOUNT"],
    created_file="reboot-request"
    )

# Script handling result reporting, in restraint compatible fashion
TMT_REPORT_RESULT_SCRIPT = ScriptCreatingFile(
    path="/usr/local/bin/tmt-report-result",
    aliases=[
        "/usr/local/bin/rstrnt-report-result",
        "/usr/local/bin/rhts-report-result"],
    related_variables=[],
    created_file="restraint-result"
    )

# Script for archiving a file, usable for BEAKERLIB_COMMAND_SUBMIT_LOG
TMT_FILE_SUBMIT_SCRIPT = Script(
    path="/usr/local/bin/tmt-file-submit",
    aliases=[
        "/usr/local/bin/rstrnt-report-log",
        "/usr/local/bin/rhts-submit-log",
        "/usr/local/bin/rhts_submit_log"],
    related_variables=[]
    )

# Script handling text execution abortion, in restraint compatible fashion
TMT_ABORT_SCRIPT = ScriptCreatingFile(
    path="/usr/local/bin/tmt-abort",
    aliases=[
        "/usr/local/bin/rstrnt-abort",
        "/usr/local/bin/rhts-abort"],
    related_variables=[],
    created_file="abort"
    )

# List of all available scripts
SCRIPTS = (
    TMT_ABORT_SCRIPT,
    TMT_FILE_SUBMIT_SCRIPT,
    TMT_REBOOT_SCRIPT,
    TMT_REPORT_RESULT_SCRIPT,
    )


@dataclasses.dataclass
class ExecuteStepData(tmt.steps.WhereableStepData, tmt.steps.StepData):
    # TODO: ugly circular dependency (see tmt.base.DEFAULT_TEST_DURATION_L2)
    duration: str = '1h'
    exit_first: bool = False


class ExecutePlugin(tmt.steps.Plugin):
    """ Common parent of execute plugins """

    _data_class = ExecuteStepData

    # List of all supported methods aggregated from all plugins of the same step.
    _supported_methods: List[tmt.steps.Method] = []

    # Internal executor is the default implementation
    how = 'tmt'

    scripts: Tuple['Script', ...] = ()

    @classmethod
    def base_command(
            cls,
            usage: str,
            method_class: Optional[Type[click.Command]] = None) -> click.Command:
        """ Create base click command (common for all execute plugins) """

        # Prepare general usage message for the step
        if method_class:
            usage = Execute.usage(method_overview=usage)

        # Create the command
        @click.command(cls=method_class, help=usage)
        @click.pass_context
        @click.option(
            '-h', '--how', metavar='METHOD',
            help='Use specified method for test execution.')
        def execute(context: click.Context, **kwargs: Any) -> None:
            context.obj.steps.add('execute')
            Execute._save_context(context)

        return execute

    @classmethod
    def options(cls, how: Optional[str] = None) -> List[tmt.options.ClickOptionDecoratorType]:
        # Add option to exit after the first test failure
        options = super().options(how)
        options[:0] = [
            click.option(
                '-x', '--exit-first', is_flag=True,
                help='Stop execution after the first test failure.'),
            ]
        return options

    def go(self, guest: Guest) -> None:
        super().go(guest)
        self.verbose(
            'exit-first', self.get('exit-first', default=False),
            'green', level=2)

    @property
    def discover(self) -> tmt.steps.discover.Discover:
        """ Return discover plugin instance """
        # This is necessary so that upgrade plugin can inject a fake discover

        # TODO: Mypy complains about this return value without cast(). It might
        # be related to mypy not following imports. It's possible that the cast
        # could be removed later.
        return cast(tmt.steps.discover.Discover, self.step.plan.discover)

    def data_path(
            self,
            test: "tmt.Test",
            filename: Optional[str] = None,
            full: bool = False,
            create: bool = False) -> str:
        """
        Prepare full/relative test data directory/file path

        Construct test data directory path for given test, create it
        if requested and return the full or relative path to it (if
        filename not provided) or to the given data file otherwise.
        """
        # Prepare directory path, create if requested
        assert self.step.workdir is not None
        directory = os.path.join(
            self.step.workdir, TEST_DATA, test.name.lstrip('/'))
        if create and not os.path.isdir(directory):
            os.makedirs(os.path.join(directory, TEST_DATA))
        if not filename:
            return directory
        path = os.path.join(directory, filename)
        return path if full else os.path.relpath(path, self.step.workdir)

    def prepare_tests(self) -> List["tmt.Test"]:
        """
        Prepare discovered tests for testing

        Check which tests have been discovered, for each test prepare
        the aggregated metadata in a 'metadata.yaml' file under the test
        data directory and finally return a list of discovered tests.
        """
        tests: List[tmt.Test] = self.discover.tests()
        for test in tests:
            metadata_filename = self.data_path(
                test, filename='metadata.yaml', full=True, create=True)
            self.write(
                metadata_filename, tmt.utils.dict_to_yaml(test._metadata))
        return tests

    def prepare_scripts(self, guest: "tmt.steps.provision.Guest") -> None:
        """
        Prepare additional scripts for testing
        """
        # Install all scripts on guest
        for script in self.scripts:
            source = os.path.join(
                SCRIPTS_SRC_DIR, os.path.basename(script.path))

            for dest in [script.path] + script.aliases:
                guest.push(
                    source=source,
                    destination=dest,
                    options=["-p", "--chmod=755"])

    def check_shell(self, test: "tmt.Test") -> List["tmt.Result"]:
        """ Check result of a shell test """
        # Prepare the log path
        data = {'log': self.data_path(test, TEST_OUTPUT_FILENAME),
                'duration': test.real_duration}
        # Process the exit code
        try:
            data['result'] = {0: 'pass', 1: 'fail'}[test.returncode]
        except KeyError:
            data['result'] = 'error'
            # Add note about the exceeded duration
            if test.returncode == tmt.utils.PROCESS_TIMEOUT:
                data['note'] = 'timeout'
                self.timeout_hint(test)
        return [tmt.Result(data, test=test)]

    def check_beakerlib(self, test: "tmt.Test") -> List["tmt.Result"]:
        """ Check result of a beakerlib test """
        # Initialize data, prepare log paths
        data = {'result': 'error',
                'log': [],
                'duration': test.real_duration}
        for log in [TEST_OUTPUT_FILENAME, 'journal.txt']:
            if os.path.isfile(self.data_path(test, log, full=True)):
                data['log'].append(self.data_path(test, log))
        # Check beakerlib log for the result
        try:
            beakerlib_results_file = self.data_path(
                test, 'TestResults', full=True)
            results = self.read(beakerlib_results_file, level=3)
        except tmt.utils.FileError:
            self.debug(f"Unable to read '{beakerlib_results_file}'.", level=3)
            data['note'] = 'beakerlib: TestResults FileError'
            return [tmt.Result(data, test=test)]

        search_result = re.search('TESTRESULT_RESULT_STRING=(.*)', results)
        # States are: started, incomplete and complete
        # FIXME In quotes until beakerlib/beakerlib/pull/92 is merged
        search_state = re.search(r'TESTRESULT_STATE="?(\w+)"?', results)

        if search_result is None or search_state is None:
            self.debug(
                f"No result or state found in '{beakerlib_results_file}'.",
                level=3)
            data['note'] = 'beakerlib: Result/State missing'
            return [tmt.Result(data, test=test)]

        result = search_result.group(1)
        state = search_state.group(1)

        # Check if it was killed by timeout (set by tmt executor)
        if test.returncode == tmt.utils.PROCESS_TIMEOUT:
            data['result'] = 'error'
            data['note'] = 'timeout'
            self.timeout_hint(test)
        # Test results should be in complete state
        elif state != 'complete':
            data['result'] = 'error'
            data['note'] = f"beakerlib: State '{state}'"
        # Finally we have a valid result
        else:
            data['result'] = result.lower()
        return [tmt.Result(data, test=test)]

    def check_result_file(self, test: "tmt.Test") -> List["tmt.Result"]:
        """
        Check result file created by tmt-report-result

        Extract the test result from the result file if it exists and
        return a Result instance. Raise the FileError exception when no
        test result file is found.
        """
        report_result_path = os.path.join(
            self.data_path(test, full=True),
            tmt.steps.execute.TEST_DATA,
            TMT_REPORT_RESULT_SCRIPT.created_file)

        # Nothing to do if there's no result file
        if not os.path.exists(report_result_path):
            raise tmt.utils.FileError

        # Prepare the log path and duration
        data = {'log': self.data_path(test, TEST_OUTPUT_FILENAME),
                'duration': test.real_duration}

        # Check the test result
        self.debug("The report-result output file detected.", level=3)
        with open(report_result_path) as result_file:
            result_list = [line for line in result_file.readlines() if "TESTRESULT" in line]
        if not result_list:
            raise tmt.utils.ExecuteError(
                f"Test result not found in result file '{report_result_path}'.")
        result = result_list[0].split("=")[1].strip()

        # Map the restraint result to the corresponding tmt value
        try:
            result_map = {
                "PASS": "pass",
                "FAIL": "fail",
                "SKIP": "info",
                "WARN": "warn",
                }
            data['result'] = result_map[result]
        except KeyError:
            data['result'] = "error"
            data['note'] = f"invalid test result '{result}' in result file"
        return [tmt.Result(data, test=test)]

    def check_custom_results(self, test: "tmt.Test") -> List["tmt.Result"]:
        """
        Process custom results.yaml file created by the test itself.
        """
        self.debug("Processing custom 'results.yaml' file created by the test itself.")

        custom_results_path = os.path.join(
            self.data_path(test, full=True),
            tmt.steps.execute.TEST_DATA,
            'results.yaml')

        if not os.path.exists(custom_results_path):
            # Missing results.yaml means error result, but tmt contines with other tests
            return [tmt.Result(
                data={
                    'note': f"custom results file '{custom_results_path}' not found",
                            'result': 'error'
                    },
                test=test)]

        with open(custom_results_path) as custom_results_file:
            results = tmt.utils.yaml_to_list(custom_results_file)

        custom_results = []
        for partial_result_data in results:
            partial_result = tmt.Result(partial_result_data, test=test)
            partial_result.name += partial_result_data['name']
            custom_results.append(partial_result)

        return custom_results

    def check_abort_file(self, test: "tmt.Test") -> bool:
        """
        Check for an abort file created by tmt-abort

        Returns whether an abort file is present (i.e. abort occurred).
        """
        abort_file_path = os.path.join(
            self.data_path(test, full=True),
            tmt.steps.execute.TEST_DATA,
            TMT_ABORT_SCRIPT.created_file)

        return os.path.exists(abort_file_path)

    @staticmethod
    def test_duration(start: float, end: float) -> str:
        """ Convert duration to a human readable format """
        return time.strftime("%H:%M:%S", time.gmtime(end - start))

    def timeout_hint(self, test: "tmt.Test") -> None:
        """ Append a duration increase hint to the test output """
        output = self.data_path(test, TEST_OUTPUT_FILENAME, full=True)
        self.write(
            output,
            f"\nMaximum test time '{test.duration}' exceeded.\n"
            f"Adjust the test 'duration' attribute if necessary.\n"
            f"https://tmt.readthedocs.io/en/stable/spec/tests.html#duration\n",
            mode='a', level=3)

    def results(self) -> List["tmt.Result"]:
        """ Return test results """
        raise NotImplementedError


class Execute(tmt.steps.Step):
    """
    Run tests using the specified executor.

    Note that the old execution methods 'shell.tmt' and 'beakerlib.tmt'
    have been deprecated and the backward-compatible support for them
    will be dropped in tmt-2.0.

    Use the new L1 metadata attribute 'framework' instead to specify
    which test framework should be used for execution. This allows to
    combine tests using different test frameworks in a single plan.
    """

    # Internal executor is the default implementation
    DEFAULT_HOW = 'tmt'

    _plugin_base_class = ExecutePlugin

    def __init__(self, plan: "tmt.Plan", data: tmt.steps.RawStepDataArgument) -> None:
        """ Initialize execute step data """
        super().__init__(plan=plan, data=data)
        # List of Result() objects representing test results
        self._results: List[tmt.Result] = []

        # Default test framework and mapping old methods
        # FIXME remove when we drop the old execution methods
        self._framework = DEFAULT_FRAMEWORK

    def _map_old_methods(self, data: List[tmt.steps.StepData]) -> None:
        """ Map the old execute methods in a backward-compatible way """
        how = data[0].how
        matched = re.search(r"^(shell|beakerlib)(\.tmt)?$", how)
        if not matched:
            return
        # Show the old method deprecation warning to users
        self.warn(f"The '{how}' execute method has been deprecated.")
        # Map the old syntax to the appropriate executor
        # shell, beakerlib ---> tmt
        # shell.tmt, beakerlib.tmt ---> tmt
        how = 'tmt'
        self.warn(f"Use 'how: {how}' in the execute step instead (L2).")
        data[0].how = how
        # Store shell or beakerlib as the default test framework
        # (used when the framework is not defined in the L1 metadata)
        framework = matched.group(1)
        self.warn(f"Set 'framework: {framework}' in test metadata (L1).")
        self._framework = framework
        self.warn("Support for old methods will be dropped in tmt-2.0.")

    def _normalize_data(self, raw_data: List[tmt.steps._RawStepData]) -> List[tmt.steps.StepData]:
        data = super()._normalize_data(raw_data)

        # Map old methods now if there is no run (and thus no wake up)
        # TODO: if not self.plan.my_run:
        self._map_old_methods(data)

        return data

    def load(self) -> None:
        """ Load test results """
        super().load()
        try:
            results = tmt.utils.yaml_to_dict(self.read('results.yaml'))
            self._results = [
                tmt.Result(data, name=test) for test, data in results.items()]
        except tmt.utils.FileError:
            self.debug('Test results not found.', level=2)

    def save(self) -> None:
        """ Save test results to the workdir """
        super().save()
        results = dict([
            (result.name, result.export()) for result in self.results()])
        self.write('results.yaml', tmt.utils.dict_to_yaml(results))

    def wake(self) -> None:
        """ Wake up the step (process workdir and command line) """
        super().wake()

        # There should be just a single definition
        if len(self.data) > 1:
            raise tmt.utils.SpecificationError(
                "Multiple execute steps defined in '{}'.".format(self.plan))

        # Choose the right plugin and wake it up
        # TODO: with generic BasePlugin, delegate() should return more fitting type,
        # not the base class.
        executor = cast(ExecutePlugin, ExecutePlugin.delegate(self, data=self.data[0]))
        executor.wake()
        self._phases.append(executor)

        # Nothing more to do if already done
        if self.status() == 'done':
            self.debug(
                'Execute wake up complete (already done before).', level=2)
        # Save status and step data (now we know what to do)
        else:
            self.status('todo')
            self.save()

    def show(self) -> None:
        """ Show execute details """
        ExecutePlugin.delegate(self, data=self.data[0]).show()

    def summary(self) -> None:
        """ Give a concise summary of the execution """
        tests = fmf.utils.listed(self.results(), 'test')
        self.info('summary', f'{tests} executed', 'green', shift=1)

    def go(self) -> None:
        """ Execute tests """
        super().go()

        # Nothing more to do if already done
        if self.status() == 'done':
            self.info('status', 'done', 'green', shift=1)
            self.summary()
            self.actions()
            return

        # Make sure that guests are prepared
        if not self.plan.provision.guests():
            raise tmt.utils.ExecuteError("No guests available for execution.")

        # Execute the tests, store results
        for guest in self.plan.provision.guests():
            for phase in self.phases(classes=(Action, ExecutePlugin)):
                if not phase.enabled_on_guest(guest):
                    continue

                if isinstance(phase, Action):
                    phase.go()

                elif isinstance(phase, ExecutePlugin):
                    phase.go(guest)

                    self._results.extend(phase.results())

                else:
                    raise GeneralError(f'Unexpected phase in execute step: {phase}')

        # Give a summary, update status and save
        self.summary()
        self.status('done')
        self.save()

    def requires(self) -> List[str]:
        """
        Packages required for test execution

        Return a list of packages which need to be installed on the
        guest so that tests can be executed. Used by the prepare step.
        """
        requires = set()
        for plugin in self.phases(classes=ExecutePlugin):
            requires.update(plugin.requires())
        return list(requires)

    def results(self) -> List["tmt.base.Result"]:
        """
        Results from executed tests

        Return a dictionary with test results according to the spec:
        https://tmt.readthedocs.io/en/latest/spec/plans.html#execute
        """
        return self._results
