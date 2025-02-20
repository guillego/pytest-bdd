import os.path
import re
import textwrap
import typing
from collections import OrderedDict

from . import exceptions, types

SPLIT_LINE_RE = re.compile(r"(?<!\\)\|")
STEP_PARAM_RE = re.compile(r"<(.+?)>")
COMMENT_RE = re.compile(r"(^|(?<=\s))#")
STEP_PREFIXES = [
    ("Feature: ", types.FEATURE),
    ("Scenario Outline: ", types.SCENARIO_OUTLINE),
    ("Examples:", types.EXAMPLES),
    ("Scenario: ", types.SCENARIO),
    ("Background:", types.BACKGROUND),
    ("Given ", types.GIVEN),
    ("When ", types.WHEN),
    ("Then ", types.THEN),
    ("@", types.TAG),
    # Continuation of the previously mentioned step type
    ("And ", None),
    ("But ", None),
]


def split_line(line):
    """Split the given Examples line.

    :param str|unicode line: Feature file Examples line.

    :return: List of strings.
    """
    return [cell.replace("\\|", "|").strip() for cell in SPLIT_LINE_RE.split(line)[1:-1]]


def parse_line(line):
    """Parse step line to get the step prefix (Scenario, Given, When, Then or And) and the actual step name.

    :param line: Line of the Feature file.

    :return: `tuple` in form ("<prefix>", "<Line without the prefix>").
    """
    for prefix, _ in STEP_PREFIXES:
        if line.startswith(prefix):
            return prefix.strip(), line[len(prefix) :].strip()
    return "", line


def strip_comments(line):
    """Remove comments.

    :param str line: Line of the Feature file.

    :return: Stripped line.
    """
    res = COMMENT_RE.search(line)
    if res:
        line = line[: res.start()]
    return line.strip()


def get_step_type(line):
    """Detect step type by the beginning of the line.

    :param str line: Line of the Feature file.

    :return: SCENARIO, GIVEN, WHEN, THEN, or `None` if can't be detected.
    """
    for prefix, _type in STEP_PREFIXES:
        if line.startswith(prefix):
            return _type


def parse_feature(basedir: str, filename: str, encoding: str = "utf-8") -> "Feature":
    """Parse the feature file.

    :param str basedir: Feature files base directory.
    :param str filename: Relative path to the feature file.
    :param str encoding: Feature file encoding (utf-8 by default).
    """
    abs_filename = os.path.abspath(os.path.join(basedir, filename))
    rel_filename = os.path.join(os.path.basename(basedir), filename)
    feature = Feature(
        scenarios=OrderedDict(),
        filename=abs_filename,
        rel_filename=rel_filename,
        line_number=1,
        name=None,
        tags=set(),
        background=None,
        description="",
    )
    scenario: typing.Optional[ScenarioTemplate] = None
    mode = None
    prev_mode = None
    description: typing.List[str] = []
    step = None
    multiline_step = False
    prev_line = None

    with open(abs_filename, encoding=encoding) as f:
        content = f.read()

    for line_number, line in enumerate(content.splitlines(), start=1):
        unindented_line = line.lstrip()
        line_indent = len(line) - len(unindented_line)
        if step and (step.indent < line_indent or ((not unindented_line) and multiline_step)):
            multiline_step = True
            # multiline step, so just add line and continue
            step.add_line(line)
            continue
        else:
            step = None
            multiline_step = False
        stripped_line = line.strip()
        clean_line = strip_comments(line)
        if not clean_line and (not prev_mode or prev_mode not in types.FEATURE):
            continue
        mode = get_step_type(clean_line) or mode

        allowed_prev_mode = (types.BACKGROUND, types.GIVEN, types.WHEN)

        if not scenario and prev_mode not in allowed_prev_mode and mode in types.STEP_TYPES:
            raise exceptions.FeatureError(
                "Step definition outside of a Scenario or a Background", line_number, clean_line, filename
            )

        if mode == types.FEATURE:
            if prev_mode is None or prev_mode == types.TAG:
                _, feature.name = parse_line(clean_line)
                feature.line_number = line_number
                feature.tags = get_tags(prev_line)
            elif prev_mode == types.FEATURE:
                description.append(clean_line)
            else:
                raise exceptions.FeatureError(
                    "Multiple features are not allowed in a single feature file",
                    line_number,
                    clean_line,
                    filename,
                )

        prev_mode = mode

        # Remove Feature, Given, When, Then, And
        keyword, parsed_line = parse_line(clean_line)
        if mode in [types.SCENARIO, types.SCENARIO_OUTLINE]:
            tags = get_tags(prev_line)
            feature.scenarios[parsed_line] = scenario = ScenarioTemplate(
                feature=feature, name=parsed_line, line_number=line_number, tags=tags
            )
        elif mode == types.BACKGROUND:
            feature.background = Background(feature=feature, line_number=line_number)
        elif mode == types.EXAMPLES:
            mode = types.EXAMPLES_HEADERS
            scenario.examples.line_number = line_number
        elif mode == types.EXAMPLES_HEADERS:
            scenario.examples.set_param_names([l for l in split_line(parsed_line) if l])
            mode = types.EXAMPLE_LINE
        elif mode == types.EXAMPLE_LINE:
            scenario.examples.add_example([l for l in split_line(stripped_line)])
        elif mode and mode not in (types.FEATURE, types.TAG):
            step = Step(name=parsed_line, type=mode, indent=line_indent, line_number=line_number, keyword=keyword)
            if feature.background and not scenario:
                target = feature.background
            else:
                target = scenario
            target.add_step(step)
        prev_line = clean_line

    feature.description = "\n".join(description).strip()
    return feature


class Feature:
    """Feature."""

    def __init__(self, scenarios, filename, rel_filename, name, tags, background, line_number, description):
        self.scenarios: typing.Dict[str, ScenarioTemplate] = scenarios
        self.rel_filename = rel_filename
        self.filename = filename
        self.tags = tags
        self.name = name
        self.line_number = line_number
        self.description = description
        self.background = background


class ScenarioTemplate:
    """A scenario template.

    Created when parsing the feature file, it will then be combined with the examples to create a Scenario."""

    def __init__(self, feature: Feature, name: str, line_number: int, tags=None):
        """

        :param str name: Scenario name.
        :param int line_number: Scenario line number.
        :param set tags: Set of tags.
        """
        self.feature = feature
        self.name = name
        self._steps: typing.List[Step] = []
        self.examples = Examples()
        self.line_number = line_number
        self.tags = tags or set()

    def add_step(self, step):
        """Add step to the scenario.

        :param pytest_bdd.parser.Step step: Step.
        """
        step.scenario = self
        self._steps.append(step)

    @property
    def steps(self):
        background = self.feature.background
        return (background.steps if background else []) + self._steps

    def render(self, context: typing.Mapping[str, typing.Any]) -> "Scenario":
        steps = [
            Step(
                name=templated_step.render(context),
                type=templated_step.type,
                indent=templated_step.indent,
                line_number=templated_step.line_number,
                keyword=templated_step.keyword,
            )
            for templated_step in self.steps
        ]
        return Scenario(feature=self.feature, name=self.name, line_number=self.line_number, steps=steps, tags=self.tags)

    def validate(self):
        """Validate the scenario.

        :raises ScenarioValidationError: when scenario is not valid
        """
        params = frozenset(sum((list(step.params) for step in self.steps), []))
        example_params = set(self.examples.example_params)
        if params and example_params and params != example_params:
            raise exceptions.ScenarioExamplesNotValidError(
                """Scenario "{}" in the feature "{}" has not valid examples. """
                """Set of step parameters {} should match set of example values {}.""".format(
                    self.name, self.feature.filename, sorted(params), sorted(example_params)
                )
            )


class Scenario:

    """Scenario."""

    def __init__(self, feature: Feature, name: str, line_number: int, steps: "typing.List[Step]", tags=None):
        """Scenario constructor.

        :param pytest_bdd.parser.Feature feature: Feature.
        :param str name: Scenario name.
        :param int line_number: Scenario line number.
        :param set tags: Set of tags.
        """
        self.feature = feature
        self.name = name
        self.steps = steps
        self.line_number = line_number
        self.tags = tags or set()
        self.failed = False


class Step:

    """Step."""

    def __init__(self, name, type, indent, line_number, keyword):
        """Step constructor.

        :param str name: step name.
        :param str type: step type.
        :param int indent: step text indent.
        :param int line_number: line number.
        :param str keyword: step keyword.
        """
        self.name = name
        self.keyword = keyword
        self.lines = []
        self.indent = indent
        self.type = type
        self.line_number = line_number
        self.failed = False
        self.start = 0
        self.stop = 0
        self.scenario = None
        self.background = None

    def add_line(self, line):
        """Add line to the multiple step.

        :param str line: Line of text - the continuation of the step name.
        """
        self.lines.append(line)

    @property
    def name(self):
        """Get step name."""
        multilines_content = textwrap.dedent("\n".join(self.lines)) if self.lines else ""

        # Remove the multiline quotes, if present.
        multilines_content = re.sub(
            pattern=r'^"""\n(?P<content>.*)\n"""$',
            repl=r"\g<content>",
            string=multilines_content,
            flags=re.DOTALL,  # Needed to make the "." match also new lines
        )

        lines = [self._name] + [multilines_content]
        return "\n".join(lines).strip()

    @name.setter
    def name(self, value):
        """Set step name."""
        self._name = value

    def __str__(self):
        """Full step name including the type."""
        return f'{self.type.capitalize()} "{self.name}"'

    @property
    def params(self):
        """Get step params."""
        return tuple(frozenset(STEP_PARAM_RE.findall(self.name)))

    def render(self, context: typing.Mapping[str, typing.Any]):
        def replacer(m: typing.Match):
            varname = m.group(1)
            return str(context[varname])

        return STEP_PARAM_RE.sub(replacer, self.name)


class Background:

    """Background."""

    def __init__(self, feature, line_number):
        """Background constructor.

        :param pytest_bdd.parser.Feature feature: Feature.
        :param int line_number: Line number.
        """
        self.feature = feature
        self.line_number = line_number
        self.steps = []

    def add_step(self, step):
        """Add step to the background."""
        step.background = self
        self.steps.append(step)


class Examples:

    """Example table."""

    def __init__(self):
        """Initialize examples instance."""
        self.example_params = []
        self.examples = []
        self.line_number = None
        self.name = None

    def set_param_names(self, keys):
        """Set parameter names.

        :param names: `list` of `string` parameter names.
        """
        self.example_params = [str(key) for key in keys]

    def add_example(self, values):
        """Add example.

        :param values: `list` of `string` parameter values.
        """
        self.examples.append(values)

    def add_example_row(self, param, values):
        """Add example row.

        :param param: `str` parameter name
        :param values: `list` of `string` parameter values
        """
        if param in self.example_params:
            raise exceptions.ExamplesNotValidError(
                f"""Example rows should contain unique parameters. "{param}" appeared more than once"""
            )
        self.example_params.append(param)

    def as_contexts(self) -> typing.Iterable[typing.Dict[str, typing.Any]]:
        if not self.examples:
            return

        header, rows = self.example_params, self.examples

        for row in rows:
            assert len(header) == len(row)
            yield dict(zip(header, row))

    def __bool__(self):
        """Bool comparison."""
        return bool(self.examples)


def get_tags(line):
    """Get tags out of the given line.

    :param str line: Feature file text line.

    :return: List of tags.
    """
    if not line or not line.strip().startswith("@"):
        return set()
    return {tag.lstrip("@") for tag in line.strip().split(" @") if len(tag) > 1}
