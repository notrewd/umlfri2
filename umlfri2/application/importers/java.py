from __future__ import annotations

import logging
import math
import os
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

import javalang
from javalang.parser import JavaSyntaxError

from umlfri2.application import Application
from umlfri2.model import ElementObject, Project
from umlfri2.model.diagram import Diagram
from umlfri2.types.geometry import Point, Size

LOGGER = logging.getLogger(__name__)

INFJAVA_UML_ADDON_ID = "urn:umlfri.org:metamodel:infjavauml"
DEFAULT_TEMPLATE_ID = "empty"
DEFAULT_DIAGRAM_TYPE = "class_diagram"
DEFAULT_CLASS_ELEMENT = "class"
DEFAULT_PACKAGE_ELEMENT = "package"
GENERALISATION_CONNECTION = "generalisation"
IMPLEMENTATION_CONNECTION = "implementation"
ASSOCIATION_CONNECTION = "association"

VISIBILITY_MAP = {
    "public": "+",
    "private": "-",
    "protected": "#",
}


class JavaImportError(Exception):
    """Raised when the Java importer cannot complete the requested action."""


@dataclass
class ImportContext:
    direct_imports: Dict[str, str] = field(default_factory=dict)
    wildcard_imports: List[str] = field(default_factory=list)


@dataclass
class TypeDescriptor:
    name: str
    qualifier: Optional[str] = None
    arguments: List["TypeDescriptor"] = field(default_factory=list)
    dimensions: int = 0

    def display(self) -> str:
        base = "".join(filter(None, [self.qualifier, "." if self.qualifier else "", self.name]))
        if not base:
            base = self.name
        if self.arguments:
            base = base + "<" + ", ".join(arg.display() for arg in self.arguments) + ">"
        if self.dimensions:
            base = base + "[]" * self.dimensions
        return base

    def direct_full_name(self) -> Optional[str]:
        if self.qualifier:
            return f"{self.qualifier}.{self.name}"
        return None

    def candidate_simple_names(self) -> Iterator[str]:
        yield self.name
        for arg in self.arguments:
            yield from arg.candidate_simple_names()


@dataclass
class JavaField:
    name: str
    type_descriptor: Optional[TypeDescriptor]
    modifiers: Set[str]

    @property
    def visibility(self) -> str:
        for modifier, value in VISIBILITY_MAP.items():
            if modifier in self.modifiers:
                return value
        return "~"

    @property
    def is_static(self) -> bool:
        return "static" in self.modifiers


@dataclass
class JavaMethodParameter:
    name: str
    type_descriptor: Optional[TypeDescriptor]


@dataclass
class JavaMethod:
    name: str
    return_type: Optional[TypeDescriptor]
    parameters: List[JavaMethodParameter]
    modifiers: Set[str]
    is_constructor: bool = False

    @property
    def visibility(self) -> str:
        for modifier, value in VISIBILITY_MAP.items():
            if modifier in self.modifiers:
                return value
        return "~"

    @property
    def is_static(self) -> bool:
        return "static" in self.modifiers

    @property
    def is_abstract(self) -> bool:
        return "abstract" in self.modifiers


@dataclass
class JavaTypeModel:
    name: str
    package: Optional[str]
    kind: str
    modifiers: Set[str]
    fields: List[JavaField]
    methods: List[JavaMethod]
    extends: List[TypeDescriptor]
    implements: List[TypeDescriptor]
    imports: ImportContext
    source_path: str
    enum_constants: List[str] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        if self.package:
            return f"{self.package}.{self.name}"
        return self.name

    @property
    def is_abstract(self) -> bool:
        return "abstract" in self.modifiers

    @property
    def stereotype(self) -> Optional[str]:
        if self.kind == "interface":
            return "interface"
        if self.kind == "enum":
            return "enum"
        return None


@dataclass
class JavaParseResult:
    types: Dict[str, JavaTypeModel]
    errors: List[str]


@dataclass
class BuildSummary:
    elements_created: int
    connections_created: int
    primary_diagram: Optional[Diagram]


@dataclass
class JavaImportReport:
    summary: BuildSummary
    warnings: List[str]


class JavaSourceParser:
    """Parse Java source files into in-memory representations."""

    def parse_files(self, paths: Sequence[str]) -> JavaParseResult:
        types: Dict[str, JavaTypeModel] = {}
        errors: List[str] = []
        for path in paths:
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    source = handle.read()
            except OSError as exc:
                message = f"{path}: cannot read file ({exc})"
                LOGGER.warning(message)
                errors.append(message)
                continue

            try:
                tree = javalang.parse.parse(source)
            except (JavaSyntaxError, IndexError, TypeError, AttributeError) as exc:
                message = f"{path}: parse error - {exc}"
                LOGGER.warning(message)
                errors.append(message)
                continue

            package = tree.package.name if tree.package else None
            imports = self._build_import_context(tree.imports)

            for type_decl in tree.types:
                model = self._convert_type(type_decl, package, imports, path)
                if model is None:
                    continue
                if model.full_name in types:
                    message = f"Duplicate type '{model.full_name}' ignored (first defined in {types[model.full_name].source_path})"
                    LOGGER.warning(message)
                    errors.append(message)
                    continue
                types[model.full_name] = model
        return JavaParseResult(types=types, errors=errors)

    def _build_import_context(self, imports: Sequence[javalang.tree.Import]) -> ImportContext:
        ctx = ImportContext()
        for imp in imports or []:
            if imp.static:
                continue
            if imp.wildcard:
                ctx.wildcard_imports.append(imp.path)
            else:
                simple = imp.path.split(".")[-1]
                ctx.direct_imports[simple] = imp.path
        return ctx

    def _convert_type(
        self,
        type_decl: javalang.tree.TypeDeclaration,
        package: Optional[str],
        imports: ImportContext,
        path: str,
    ) -> Optional[JavaTypeModel]:
        if isinstance(type_decl, javalang.tree.ClassDeclaration):
            kind = "class"
            extends = [self._convert_reference(type_decl.extends)] if type_decl.extends else []
            implements = [self._convert_reference(it) for it in type_decl.implements or []]
        elif isinstance(type_decl, javalang.tree.InterfaceDeclaration):
            kind = "interface"
            extends = [self._convert_reference(it) for it in type_decl.extends or []]
            implements = []
        elif isinstance(type_decl, javalang.tree.EnumDeclaration):
            kind = "enum"
            extends = []
            implements = [self._convert_reference(it) for it in type_decl.implements or []]
        else:
            LOGGER.debug("Unsupported type '%s' in %s", type(type_decl), path)
            return None

        modifiers = set(type_decl.modifiers or [])
        fields = self._convert_fields(type_decl)
        methods = self._convert_methods(type_decl)
        enum_constants = []
        if isinstance(type_decl, javalang.tree.EnumDeclaration):
            enum_constants = [const.name for const in type_decl.constants or []]

        return JavaTypeModel(
            name=type_decl.name,
            package=package,
            kind=kind,
            modifiers=modifiers,
            fields=fields,
            methods=methods,
            extends=[ext for ext in extends if ext is not None],
            implements=[imp for imp in implements if imp is not None],
            imports=imports,
            source_path=path,
            enum_constants=enum_constants,
        )

    def _convert_reference(self, ref: Optional[javalang.tree.Type]) -> Optional[TypeDescriptor]:
        if ref is None:
            return None
        names: List[str] = []
        current = ref
        while current is not None:
            names.append(current.name if isinstance(current.name, str) else str(current.name))
            current = getattr(current, "sub_type", None)

        qualifier = None
        base_name = names[-1]
        if len(names) > 1:
            qualifier = ".".join(names[:-1])

        arguments: List[Optional[TypeDescriptor]] = []
        raw_arguments = getattr(ref, "arguments", None) or []
        for argument in raw_arguments:
            if isinstance(argument, javalang.tree.TypeArgument):
                referenced_type = argument.type or argument.pattern_type
            else:
                referenced_type = argument
            arguments.append(self._convert_reference(referenced_type))
        filtered_arguments = [arg for arg in arguments if arg is not None]

        descriptor = TypeDescriptor(
            name=base_name,
            qualifier=qualifier,
            arguments=filtered_arguments,
            dimensions=len(getattr(ref, "dimensions", None) or []),
        )
        return descriptor

    def _convert_fields(self, type_decl: javalang.tree.TypeDeclaration) -> List[JavaField]:
        result: List[JavaField] = []
        for field in getattr(type_decl, "fields", []):
            descriptor = self._convert_reference(field.type)
            for declarator in field.declarators:
                result.append(JavaField(name=declarator.name, type_descriptor=descriptor, modifiers=set(field.modifiers or [])))
        return result

    def _convert_methods(self, type_decl: javalang.tree.TypeDeclaration) -> List[JavaMethod]:
        result: List[JavaMethod] = []
        for method in getattr(type_decl, "methods", []):
            params = [JavaMethodParameter(name=param.name, type_descriptor=self._convert_reference(param.type)) for param in method.parameters]
            result.append(
                JavaMethod(
                    name=method.name,
                    return_type=self._convert_reference(method.return_type),
                    parameters=params,
                    modifiers=set(method.modifiers or []),
                    is_constructor=False,
                )
            )
        for constructor in getattr(type_decl, "constructors", []):
            params = [JavaMethodParameter(name=param.name, type_descriptor=self._convert_reference(param.type)) for param in constructor.parameters]
            result.append(
                JavaMethod(
                    name=constructor.name,
                    return_type=None,
                    parameters=params,
                    modifiers=set(constructor.modifiers or []),
                    is_constructor=True,
                )
            )
        if isinstance(type_decl, javalang.tree.InterfaceDeclaration):
            for method in result:
                if not method.is_constructor and "static" not in method.modifiers and "default" not in method.modifiers:
                    method.modifiers.add("abstract")
        return result


class JavaTypeResolver:
    def __init__(self, types: Dict[str, JavaTypeModel]):
        self._types = types
        self._simple_index: Dict[str, Set[str]] = defaultdict(set)
        for full_name, model in types.items():
            self._simple_index[model.name].add(full_name)

    def resolve(self, descriptor: Optional[TypeDescriptor], context: JavaTypeModel) -> Optional[str]:
        if descriptor is None:
            return None
        direct = descriptor.direct_full_name()
        if direct and direct in self._types:
            return direct
        simple = descriptor.name
        direct_import = context.imports.direct_imports.get(simple)
        if direct_import in self._types:
            return direct_import
        for wildcard in context.imports.wildcard_imports:
            candidate = f"{wildcard}.{simple}"
            if candidate in self._types:
                return candidate
        if context.package:
            candidate = f"{context.package}.{simple}"
            if candidate in self._types:
                return candidate
        matches = self._simple_index.get(simple)
        if matches and len(matches) == 1:
            return next(iter(matches))
        if direct:
            return direct
        return None

    def resolve_all(self, descriptors: Iterable[TypeDescriptor], context: JavaTypeModel) -> List[str]:
        output: List[str] = []
        for descriptor in descriptors:
            resolved = self.resolve(descriptor, context)
            if resolved:
                output.append(resolved)
        return output


class JavaModelBuilder:
    BASE_ELEMENT_WIDTH = 240
    BASE_ELEMENT_HEIGHT = 140
    ELEMENT_SPACING_X = 60
    ELEMENT_SPACING_Y = 60
    def __init__(self, project: Project, ruler):
        self._project = project
        self._ruler = ruler
        self._metamodel = project.metamodel
        self._package_type = self._metamodel.get_element_type(DEFAULT_PACKAGE_ELEMENT)
        self._class_type = self._metamodel.get_element_type(DEFAULT_CLASS_ELEMENT)
        self._diagram_type = self._metamodel.get_diagram_type(DEFAULT_DIAGRAM_TYPE)
        self._generalisation_type = self._metamodel.get_connection_type(GENERALISATION_CONNECTION)
        self._implementation_type = self._metamodel.get_connection_type(IMPLEMENTATION_CONNECTION)
        self._association_type = self._metamodel.get_connection_type(ASSOCIATION_CONNECTION)
        self._packages: Dict[Tuple[str, ...], ElementObject] = {}
        self._class_elements: Dict[str, ElementObject] = {}
        self._class_visuals: List[object] = []
        self._connections: Set[Tuple[str, str, str]] = set()
        self._diagram: Optional[Diagram] = None

    def build(self, types: Dict[str, JavaTypeModel]) -> BuildSummary:
        if not types:
            raise JavaImportError("No Java types to import")
        resolver = JavaTypeResolver(types)
        root_package = self._create_root_package()
        diagram = self._ensure_root_diagram(root_package)
        self._diagram = diagram

        package_groups: Dict[Tuple[str, ...], List[JavaTypeModel]] = defaultdict(list)
        for model in types.values():
            path = tuple(model.package.split(".")) if model.package else tuple()
            package_groups[path].append(model)

        ordered_packages = sorted(package_groups.items(), key=lambda item: (len(item[0]), ".".join(item[0])))
        total_elements = 0
        for package_path, models in ordered_packages:
            package_element = self._ensure_package(package_path, root_package)
            for model in models:
                element = self._create_class_element(package_element, model)
                self._class_elements[model.full_name] = element
                self._register_class_visual(model.full_name, element)
                total_elements += 1

        self._layout_classes()
        total_connections = self._create_connections(types, resolver, diagram)
        self._project.invalidate_all_caches()
        return BuildSummary(elements_created=total_elements, connections_created=total_connections, primary_diagram=diagram)

    def _create_root_package(self):
        package = self._project.create_child_element(self._package_type)
        mutable = package.data.make_mutable()
        mutable.set_value("name", self._project.name or "Imported Model")
        package.apply_ufl_patch(mutable.make_patch())
        self._packages[tuple()] = package
        return package

    def _ensure_package(self, path: Tuple[str, ...], root_package):
        if not path:
            return self._packages[tuple()]
        current = root_package
        built_path: List[str] = []
        for segment in path:
            built_path.append(segment)
            tuple_path = tuple(built_path)
            if tuple_path not in self._packages:
                package = current.create_child_element(self._package_type)
                mutable = package.data.make_mutable()
                mutable.set_value("name", segment)
                package.apply_ufl_patch(mutable.make_patch())
                self._packages[tuple_path] = package
            current = self._packages[tuple_path]
        return current

    def _ensure_root_diagram(self, root_package):
        diagram = root_package.create_child_diagram(self._diagram_type)
        mutable = diagram.data.make_mutable()
        mutable.set_value("name", "Imported Java Classes")
        diagram.apply_ufl_patch(mutable.make_patch())
        return diagram

    def _create_class_element(self, parent, model: JavaTypeModel):
        element = parent.create_child_element(self._class_type)
        mutable = element.data.make_mutable()
        mutable.set_value("name", model.name)
        mutable.set_value("abstract", model.is_abstract)
        if model.stereotype:
            mutable.set_value("stereotype", model.stereotype)
        if model.kind == "enum" and model.enum_constants:
            attributes = mutable.get_value("attributes")
            for const in model.enum_constants:
                row = attributes.append()
                row.set_value("name", const)
                row.set_value("visibility", VISIBILITY_MAP.get("public", "+"))
        else:
            self._populate_attributes(mutable, model.fields)
        self._populate_operations(mutable, model.methods)
        element.apply_ufl_patch(mutable.make_patch())
        return element

    def _register_class_visual(self, class_name: str, element):
        if self._diagram is None:
            raise JavaImportError("Diagram is not initialized")

        visual = self._diagram.show(element)
        self._class_visuals.append(visual)

    def _populate_attributes(self, mutable, fields: List[JavaField]):
        attributes = mutable.get_value("attributes")
        for field in fields:
            row = attributes.append()
            row.set_value("name", field.name)
            row.set_value("type", field.type_descriptor.display() if field.type_descriptor else "")
            row.set_value("visibility", field.visibility)
            row.set_value("static", field.is_static)

    def _populate_operations(self, mutable, methods: List[JavaMethod]):
        operations = mutable.get_value("operations")
        for method in methods:
            row = operations.append()
            row.set_value("name", method.name)
            row.set_value("rtype", method.return_type.display() if method.return_type else "")
            row.set_value("visibility", method.visibility)
            row.set_value("static", method.is_static)
            row.set_value("abstract", method.is_abstract)
            params = row.get_value("parameters")
            for param in method.parameters:
                prow = params.append()
                prow.set_value("name", param.name)
                prow.set_value("type", param.type_descriptor.display() if param.type_descriptor else "")

    def _layout_classes(self):
        if not self._class_visuals:
            return

        count = len(self._class_visuals)
        columns = max(1, int(math.sqrt(count)))
        rows = math.ceil(count / columns)

        column_widths = [self.BASE_ELEMENT_WIDTH for _ in range(columns)]
        row_heights = [self.BASE_ELEMENT_HEIGHT for _ in range(rows)]
        layout_entries = []

        for idx, visual in enumerate(self._class_visuals):
            column = idx % columns
            row = idx // columns
            minimal = visual.get_minimal_size(self._ruler)
            width = max(self.BASE_ELEMENT_WIDTH, minimal.width)
            height = max(self.BASE_ELEMENT_HEIGHT, minimal.height)
            column_widths[column] = max(column_widths[column], width)
            row_heights[row] = max(row_heights[row], height)
            layout_entries.append((visual, width, height, column, row))

        column_offsets = []
        offset = 0
        for width in column_widths:
            column_offsets.append(offset)
            offset += width + self.ELEMENT_SPACING_X

        row_offsets = []
        offset = 0
        for height in row_heights:
            row_offsets.append(offset)
            offset += height + self.ELEMENT_SPACING_Y

        origin_x = 40
        origin_y = 40

        for visual, width, height, column, row in layout_entries:
            x = origin_x + column_offsets[column]
            y = origin_y + row_offsets[row]
            visual.move(self._ruler, Point(x, y))
            visual.resize(self._ruler, Size(width, height))

    def _create_connections(self, types: Dict[str, JavaTypeModel], resolver: JavaTypeResolver, diagram) -> int:
        created = 0
        for model in types.values():
            source = self._class_elements.get(model.full_name)
            if source is None:
                continue
            for base in model.extends:
                target_name = resolver.resolve(base, model)
                target = self._class_elements.get(target_name)
                if target:
                    created += self._ensure_connection(source, target, self._generalisation_type, diagram)
            for iface in model.implements:
                target_name = resolver.resolve(iface, model)
                target = self._class_elements.get(target_name)
                if target:
                    connection_type = self._implementation_type if model.kind == "class" else self._generalisation_type
                    created += self._ensure_connection(source, target, connection_type, diagram)
            self._create_associations(model, resolver, source, diagram)
        return created

    def _create_associations(self, model: JavaTypeModel, resolver: JavaTypeResolver, source, diagram):
        for field in model.fields:
            descriptor = field.type_descriptor
            if descriptor is None:
                continue
            association_targets = self._resolve_field_targets(descriptor, resolver, model)
            for target_name in association_targets:
                target = self._class_elements.get(target_name)
                if target:
                    self._ensure_connection(source, target, self._association_type, diagram)

    def _resolve_field_targets(self, descriptor: TypeDescriptor, resolver: JavaTypeResolver, context: JavaTypeModel) -> Set[str]:
        targets: Set[str] = set()
        direct = resolver.resolve(descriptor, context)
        if direct:
            targets.add(direct)
        for argument in descriptor.arguments:
            resolved = resolver.resolve(argument, context)
            if resolved:
                targets.add(resolved)
        return targets

    def _ensure_connection(self, source, target, connection_type, diagram) -> int:
        key = (connection_type.id, str(source.save_id), str(target.save_id))
        if key in self._connections:
            return 0
        connection = source.connect_with(connection_type, target)
        mutable = connection.data.make_mutable()
        connection.apply_ufl_patch(mutable.make_patch())
        diagram.show(connection)
        self._connections.add(key)
        return 1


class JavaImportController:
    """Public entry point used by the UI to import Java code."""

    def __init__(self, application: Optional[Application] = None, addon_identifier: str = INFJAVA_UML_ADDON_ID,
                 template_id: str = DEFAULT_TEMPLATE_ID):
        self._application = application or Application()
        self._addon_identifier = addon_identifier
        self._template_id = template_id
        self._parser = JavaSourceParser()

    def import_directory(self, path: str, project_name: Optional[str] = None) -> JavaImportReport:
        normalized = os.path.abspath(path)
        if not os.path.exists(normalized):
            raise JavaImportError(f"Path '{path}' does not exist")

        java_files = self._collect_java_files(normalized)
        if not java_files:
            raise JavaImportError("No .java files were found in the selected location")

        parse_result = self._parser.parse_files(java_files)
        if not parse_result.types:
            raise JavaImportError("Could not parse any Java types. Check the warnings for details.")

        addon = self._application.addons.local.get_addon(self._addon_identifier)
        if addon is None or addon.metamodel is None:
            raise JavaImportError("Required infjavauml metamodel is not available")

        template = self._find_template(addon.metamodel.templates)
        if template is None:
            raise JavaImportError(f"Template '{self._template_id}' was not found in the metamodel")

        name = project_name or os.path.basename(normalized) or "Imported Java Project"
        self._application.new_project(template, new_solution=True, project_name=name)
        project = next(self._application.solution.children)

        builder = JavaModelBuilder(project, self._application.ruler)
        summary = builder.build(parse_result.types)
        if summary.primary_diagram is not None:
            self._application.tabs.select_tab(summary.primary_diagram)
        return JavaImportReport(summary=summary, warnings=parse_result.errors)

    def _collect_java_files(self, path: str) -> List[str]:
        if os.path.isfile(path) and path.endswith(".java"):
            return [path]
        files: List[str] = []
        for root, _, filenames in os.walk(path):
            for filename in filenames:
                if filename.endswith(".java"):
                    files.append(os.path.join(root, filename))
        return sorted(files)

    def _find_template(self, templates) -> Optional[object]:
        for template in templates:
            if template.id == self._template_id:
                return template
        return None
