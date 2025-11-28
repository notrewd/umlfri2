from __future__ import annotations

import ast
import logging
import os
from typing import Dict, List, Optional, Set

from .base import (
    BaseImportController, BaseModelBuilder, ImportError, ImportReport, ImportView,
    ParseResult, TypeModel, FieldModel, MethodModel, MethodParameter, TypeDescriptor,
    INFJAVA_UML_ADDON_ID, DEFAULT_TEMPLATE_ID,
)

LOGGER = logging.getLogger(__name__)


class PythonImportError(ImportError):
    """Raised when the Python importer cannot complete the requested action."""


class PythonSourceParser:
    """Parse Python source files into in-memory representations using the ast module."""

    def parse_files(self, paths: List[str]) -> ParseResult:
        types: Dict[str, TypeModel] = {}
        errors: List[str] = []
        
        for path in paths:
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except OSError as exc:
                errors.append(f"Cannot read file {path}: {exc}")
                continue

            try:
                tree = ast.parse(content, filename=path)
                file_types = self._extract_classes(tree, path)
                for type_model in file_types:
                    types[type_model.full_name] = type_model
            except SyntaxError as exc:
                errors.append(f"Syntax error in {path}: {exc}")
                continue
            except Exception as exc:
                errors.append(f"Error parsing {path}: {exc}")
                continue

        return ParseResult(types=types, errors=errors)

    def _extract_classes(self, tree: ast.Module, path: str) -> List[TypeModel]:
        result: List[TypeModel] = []
        
        # Determine module name from path
        module_name = self._get_module_name(path)
        
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                type_model = self._convert_class(node, module_name, path)
                if type_model:
                    result.append(type_model)
        
        return result

    def _get_module_name(self, path: str) -> Optional[str]:
        """Extract module name from file path."""
        basename = os.path.basename(path)
        if basename == "__init__.py":
            return os.path.basename(os.path.dirname(path))
        return os.path.splitext(basename)[0]

    def _convert_class(self, node: ast.ClassDef, module_name: Optional[str], path: str) -> Optional[TypeModel]:
        name = node.name
        
        # Determine kind based on decorators and naming conventions
        kind = "class"
        modifiers: Set[str] = set()
        
        for decorator in node.decorator_list:
            decorator_name = self._get_decorator_name(decorator)
            if decorator_name == "abstractmethod" or decorator_name == "ABC":
                modifiers.add("abstract")
            elif decorator_name == "dataclass":
                modifiers.add("dataclass")
        
        # Check if it's an abstract class
        for base in node.bases:
            base_name = self._get_base_name(base)
            if base_name in ("ABC", "ABCMeta"):
                modifiers.add("abstract")
        
        # Check for Enum base class
        for base in node.bases:
            base_name = self._get_base_name(base)
            if base_name and "Enum" in base_name:
                kind = "enum"
                break
        
        # Extract base classes
        extends: List[TypeDescriptor] = []
        for base in node.bases:
            base_name = self._get_base_name(base)
            if base_name and base_name not in ("object", "ABC", "Enum", "IntEnum", "StrEnum"):
                extends.append(TypeDescriptor(name=base_name))
        
        # Extract fields and methods
        fields: List[FieldModel] = []
        methods: List[MethodModel] = []
        enum_constants: List[str] = []
        
        for item in node.body:
            if isinstance(item, ast.FunctionDef) or isinstance(item, ast.AsyncFunctionDef):
                method = self._convert_method(item, name)
                if method:
                    methods.append(method)
            elif isinstance(item, ast.AnnAssign):
                # Class-level annotated assignment (e.g., x: int = 5)
                field = self._convert_annotated_field(item)
                if field:
                    fields.append(field)
            elif isinstance(item, ast.Assign):
                # Class-level assignment - could be enum values or class attributes
                if kind == "enum":
                    for target in item.targets:
                        if isinstance(target, ast.Name):
                            enum_constants.append(target.id)
                else:
                    for target in item.targets:
                        if isinstance(target, ast.Name):
                            fields.append(FieldModel(
                                name=target.id,
                                type_descriptor=None,
                                modifiers={"public"} if not target.id.startswith("_") else {"private"},
                            ))
        
        # Extract instance attributes from __init__
        init_method = next((m for m in methods if m.is_constructor), None)
        if init_method:
            init_fields = self._extract_init_fields(node)
            for field in init_fields:
                if not any(f.name == field.name for f in fields):
                    fields.append(field)
        
        return TypeModel(
            name=name,
            package=module_name,
            kind=kind,
            modifiers=modifiers,
            fields=fields,
            methods=methods,
            extends=extends,
            implements=[],
            source_path=path,
            enum_constants=enum_constants,
        )

    def _get_decorator_name(self, decorator) -> Optional[str]:
        if isinstance(decorator, ast.Name):
            return decorator.id
        elif isinstance(decorator, ast.Attribute):
            return decorator.attr
        elif isinstance(decorator, ast.Call):
            return self._get_decorator_name(decorator.func)
        return None

    def _get_base_name(self, base) -> Optional[str]:
        if isinstance(base, ast.Name):
            return base.id
        elif isinstance(base, ast.Attribute):
            return base.attr
        elif isinstance(base, ast.Subscript):
            # Generic type like List[int]
            return self._get_base_name(base.value)
        return None

    def _convert_method(self, node, class_name: str) -> Optional[MethodModel]:
        name = node.name
        
        # Skip private methods in some cases
        modifiers: Set[str] = set()
        if name.startswith("__") and name.endswith("__"):
            modifiers.add("public")  # Dunder methods are public
        elif name.startswith("__"):
            modifiers.add("private")
        elif name.startswith("_"):
            modifiers.add("protected")
        else:
            modifiers.add("public")
        
        # Check decorators
        is_static = False
        is_classmethod = False
        is_abstract = False
        is_property = False
        
        for decorator in node.decorator_list:
            decorator_name = self._get_decorator_name(decorator)
            if decorator_name == "staticmethod":
                is_static = True
                modifiers.add("static")
            elif decorator_name == "classmethod":
                is_classmethod = True
                modifiers.add("static")
            elif decorator_name == "abstractmethod":
                is_abstract = True
                modifiers.add("abstract")
            elif decorator_name == "property":
                is_property = True
        
        # Skip properties for now (they're more like fields)
        if is_property:
            return None
        
        # Get return type
        return_type = None
        if node.returns:
            return_type = self._convert_annotation(node.returns)
        
        # Get parameters
        parameters: List[MethodParameter] = []
        args = node.args
        
        # Skip 'self' or 'cls' parameter
        skip_first = not is_static
        
        for i, arg in enumerate(args.args):
            if skip_first and i == 0:
                continue
            param_type = None
            if arg.annotation:
                param_type = self._convert_annotation(arg.annotation)
            parameters.append(MethodParameter(
                name=arg.arg,
                type_descriptor=param_type,
            ))
        
        is_constructor = name == "__init__"
        
        return MethodModel(
            name=name if not is_constructor else class_name,
            return_type=return_type,
            parameters=parameters,
            modifiers=modifiers,
            is_constructor=is_constructor,
        )

    def _convert_annotated_field(self, node: ast.AnnAssign) -> Optional[FieldModel]:
        if not isinstance(node.target, ast.Name):
            return None
        
        name = node.target.id
        modifiers: Set[str] = set()
        
        if name.startswith("__"):
            modifiers.add("private")
        elif name.startswith("_"):
            modifiers.add("protected")
        else:
            modifiers.add("public")
        
        type_descriptor = self._convert_annotation(node.annotation)
        
        return FieldModel(
            name=name,
            type_descriptor=type_descriptor,
            modifiers=modifiers,
        )

    def _extract_init_fields(self, class_node: ast.ClassDef) -> List[FieldModel]:
        """Extract instance attributes from __init__ method."""
        fields: List[FieldModel] = []
        
        for item in class_node.body:
            if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                for stmt in ast.walk(item):
                    if isinstance(stmt, ast.Assign):
                        for target in stmt.targets:
                            if isinstance(target, ast.Attribute):
                                if isinstance(target.value, ast.Name) and target.value.id == "self":
                                    name = target.attr
                                    modifiers: Set[str] = set()
                                    if name.startswith("__"):
                                        modifiers.add("private")
                                    elif name.startswith("_"):
                                        modifiers.add("protected")
                                    else:
                                        modifiers.add("public")
                                    
                                    # Detect if field is instantiated with a constructor call
                                    is_instantiated = isinstance(stmt.value, ast.Call)
                                    
                                    fields.append(FieldModel(
                                        name=name,
                                        type_descriptor=self._infer_type_from_value(stmt.value),
                                        modifiers=modifiers,
                                        is_instantiated=is_instantiated,
                                    ))
                    elif isinstance(stmt, ast.AnnAssign):
                        if isinstance(stmt.target, ast.Attribute):
                            if isinstance(stmt.target.value, ast.Name) and stmt.target.value.id == "self":
                                name = stmt.target.attr
                                modifiers: Set[str] = set()
                                if name.startswith("__"):
                                    modifiers.add("private")
                                elif name.startswith("_"):
                                    modifiers.add("protected")
                                else:
                                    modifiers.add("public")
                                
                                # Detect if field is instantiated with a constructor call
                                is_instantiated = stmt.value is not None and isinstance(stmt.value, ast.Call)
                                
                                fields.append(FieldModel(
                                    name=name,
                                    type_descriptor=self._convert_annotation(stmt.annotation),
                                    modifiers=modifiers,
                                    is_instantiated=is_instantiated,
                                ))
                break
        
        return fields

    def _convert_annotation(self, annotation) -> Optional[TypeDescriptor]:
        if annotation is None:
            return None
        
        if isinstance(annotation, ast.Name):
            return TypeDescriptor(name=annotation.id)
        elif isinstance(annotation, ast.Constant):
            # String annotation (forward reference)
            if isinstance(annotation.value, str):
                return TypeDescriptor(name=annotation.value)
        elif isinstance(annotation, ast.Subscript):
            # Generic type like List[int], Dict[str, int], Optional[str]
            base_name = self._get_base_name(annotation.value)
            if base_name:
                args = self._get_subscript_args(annotation.slice)
                return TypeDescriptor(
                    name=base_name,
                    arguments=[self._convert_annotation(arg) for arg in args if arg],
                )
        elif isinstance(annotation, ast.Attribute):
            # Qualified name like typing.List
            return TypeDescriptor(name=annotation.attr)
        elif isinstance(annotation, ast.BinOp):
            # Union type with | operator (Python 3.10+)
            if isinstance(annotation.op, ast.BitOr):
                left = self._convert_annotation(annotation.left)
                right = self._convert_annotation(annotation.right)
                if left and right:
                    return TypeDescriptor(
                        name="Union",
                        arguments=[left, right],
                    )
        
        return None

    def _get_subscript_args(self, slice_node) -> List:
        """Extract arguments from a subscript slice."""
        if isinstance(slice_node, ast.Tuple):
            return list(slice_node.elts)
        else:
            return [slice_node]

    def _infer_type_from_value(self, value) -> Optional[TypeDescriptor]:
        """Try to infer the type from an assignment value."""
        if value is None:
            return None
        
        if isinstance(value, ast.Call):
            # Constructor call - get the class name
            if isinstance(value.func, ast.Name):
                return TypeDescriptor(name=value.func.id)
            elif isinstance(value.func, ast.Attribute):
                return TypeDescriptor(name=value.func.attr)
        elif isinstance(value, ast.List):
            return TypeDescriptor(name="list")
        elif isinstance(value, ast.Dict):
            return TypeDescriptor(name="dict")
        elif isinstance(value, ast.Set):
            return TypeDescriptor(name="set")
        elif isinstance(value, ast.Constant):
            if isinstance(value.value, str):
                return TypeDescriptor(name="str")
            elif isinstance(value.value, int):
                return TypeDescriptor(name="int")
            elif isinstance(value.value, float):
                return TypeDescriptor(name="float")
            elif isinstance(value.value, bool):
                return TypeDescriptor(name="bool")
        
        return None


class PythonImportController(BaseImportController):
    """Public entry point used by the UI to import Python code."""

    def __init__(self, application=None, addon_identifier: str = INFJAVA_UML_ADDON_ID,
                 template_id: str = DEFAULT_TEMPLATE_ID):
        super().__init__(application, addon_identifier, template_id)
        self._parser = PythonSourceParser()

    def import_directory(self, path: str, project_name: Optional[str] = None,
                         view: ImportView = ImportView.INTERNAL) -> ImportReport:
        normalized = os.path.abspath(path)
        if not os.path.exists(normalized):
            raise PythonImportError(f"Path does not exist: {normalized}")

        py_files = self._collect_files(normalized, ".py")
        if not py_files:
            raise PythonImportError("No Python files found in the specified directory")

        parse_result = self._parser.parse_files(py_files)
        if not parse_result.types:
            raise PythonImportError("No Python classes could be parsed from the files")

        name = project_name or os.path.basename(normalized) or "Imported Python Project"
        project = self._create_project(name)

        builder = BaseModelBuilder(project, self._application.ruler, view=view)
        summary = builder.build(parse_result.types)
        if summary.primary_diagram is not None:
            self._application.tabs.select_tab(summary.primary_diagram)
        return ImportReport(summary=summary, warnings=parse_result.errors)
