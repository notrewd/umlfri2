from __future__ import annotations

import logging
import os
import re
from typing import Dict, List, Optional, Set

from .base import (
    BaseImportController, BaseModelBuilder, ImportError, ImportReport, ImportView,
    ParseResult, TypeModel, FieldModel, MethodModel, MethodParameter, TypeDescriptor,
    INFJAVA_UML_ADDON_ID, DEFAULT_TEMPLATE_ID,
)

LOGGER = logging.getLogger(__name__)


class CSharpImportError(ImportError):
    """Raised when the C# importer cannot complete the requested action."""


class CSharpSourceParser:
    """Parse C# source files into in-memory representations using regex-based parsing."""

    # Regular expressions for C# parsing
    NAMESPACE_PATTERN = re.compile(r'namespace\s+([\w.]+)')
    CLASS_PATTERN = re.compile(
        r'(public|private|protected|internal|abstract|sealed|static|partial|\s)+\s*'
        r'(class|interface|struct|enum)\s+(\w+)'
        r'(?:<[^>]+>)?'  # Generic parameters
        r'(?:\s*:\s*([^{]+))?'  # Base types
    )
    FIELD_PATTERN = re.compile(
        r'(public|private|protected|internal|static|readonly|const|\s)+\s+'
        r'([\w<>\[\],\s.?]+?)\s+(\w+)\s*(?:=\s*([^;]+))?;'
    )
    PROPERTY_PATTERN = re.compile(
        r'(public|private|protected|internal|static|virtual|override|abstract|\s)+\s+'
        r'([\w<>\[\],\s.?]+?)\s+(\w+)\s*\{'
    )
    METHOD_PATTERN = re.compile(
        r'(public|private|protected|internal|static|virtual|override|abstract|async|\s)+\s+'
        r'([\w<>\[\],\s.?]+?)\s+(\w+)\s*\(([^)]*)\)'
    )
    CONSTRUCTOR_PATTERN = re.compile(
        r'(public|private|protected|internal|\s)+\s+'
        r'(\w+)\s*\(([^)]*)\)\s*(?::\s*(?:base|this)\s*\([^)]*\))?\s*\{'
    )
    ENUM_VALUE_PATTERN = re.compile(r'(\w+)\s*(?:=\s*[^,]+)?[,}]')

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
                file_types = self._parse_content(content, path)
                for type_model in file_types:
                    types[type_model.full_name] = type_model
            except Exception as exc:
                errors.append(f"Error parsing {path}: {exc}")
                continue

        return ParseResult(types=types, errors=errors)

    def _parse_content(self, content: str, path: str) -> List[TypeModel]:
        result: List[TypeModel] = []
        
        # Remove comments and strings for cleaner parsing
        clean_content = self._remove_comments_and_strings(content)
        
        # Find namespace
        namespace_match = self.NAMESPACE_PATTERN.search(clean_content)
        namespace = namespace_match.group(1) if namespace_match else None
        
        # Find all type declarations
        for match in self.CLASS_PATTERN.finditer(clean_content):
            modifiers_str = match.group(1) or ""
            modifiers = set(modifiers_str.split())
            kind = match.group(2)
            name = match.group(3)
            base_types_str = match.group(4) or ""
            
            extends: List[TypeDescriptor] = []
            implements: List[TypeDescriptor] = []
            
            if base_types_str:
                for base in base_types_str.split(","):
                    base = base.strip()
                    if base:
                        descriptor = self._parse_type_reference(base)
                        if descriptor:
                            # In C#, the first base type for a class could be a class or interface
                            if kind == "interface" or base.startswith("I") and len(base) > 1 and base[1].isupper():
                                implements.append(descriptor)
                            else:
                                if not extends:
                                    extends.append(descriptor)
                                else:
                                    implements.append(descriptor)
            
            # Extract body of this type
            body_start = match.end()
            body = self._extract_body(clean_content, body_start)
            
            # For fields and methods, only parse class-level content (not nested method bodies)
            class_level_body = self._extract_class_level_content(body)
            
            fields = self._parse_fields(class_level_body, kind)
            methods = self._parse_methods(class_level_body, name, kind)
            enum_constants = self._parse_enum_constants(body) if kind == "enum" else []
            
            result.append(TypeModel(
                name=name,
                package=namespace,
                kind=kind,
                modifiers=modifiers,
                fields=fields,
                methods=methods,
                extends=extends,
                implements=implements,
                source_path=path,
                enum_constants=enum_constants,
            ))
        
        return result

    def _remove_comments_and_strings(self, content: str) -> str:
        # Remove multi-line comments
        content = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)
        # Remove single-line comments
        content = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
        # Remove string literals (simplified)
        content = re.sub(r'"(?:[^"\\]|\\.)*"', '""', content)
        content = re.sub(r"'(?:[^'\\]|\\.)*'", "''", content)
        return content

    def _extract_body(self, content: str, start: int) -> str:
        # Find matching braces
        brace_count = 0
        body_start = -1
        for i in range(start, len(content)):
            if content[i] == '{':
                if body_start == -1:
                    body_start = i + 1
                brace_count += 1
            elif content[i] == '}':
                brace_count -= 1
                if brace_count == 0:
                    return content[body_start:i]
        return ""

    def _extract_class_level_content(self, body: str) -> str:
        """Extract only top-level class content, replacing nested braces with placeholders.
        
        This ensures we only parse class-level fields and methods, not local variables
        inside method bodies.
        """
        result = []
        brace_depth = 0
        i = 0
        while i < len(body):
            char = body[i]
            if char == '{':
                brace_depth += 1
                result.append(char)
            elif char == '}':
                brace_depth -= 1
                result.append(char)
            elif brace_depth == 0:
                result.append(char)
            # Skip content inside nested braces (method bodies, property bodies, etc.)
            i += 1
        return ''.join(result)

    def _parse_type_reference(self, type_str: str) -> Optional[TypeDescriptor]:
        type_str = type_str.strip()
        if not type_str:
            return None
        
        # Handle generic types
        generic_match = re.match(r'([\w.]+)<(.+)>(\[\])?', type_str)
        if generic_match:
            name = generic_match.group(1)
            args_str = generic_match.group(2)
            dimensions = 1 if generic_match.group(3) else 0
            arguments = [self._parse_type_reference(arg.strip()) for arg in self._split_generic_args(args_str)]
            return TypeDescriptor(
                name=name.split('.')[-1],
                qualifier='.'.join(name.split('.')[:-1]) if '.' in name else None,
                arguments=[a for a in arguments if a],
                dimensions=dimensions,
            )
        
        # Handle array types
        array_match = re.match(r'([\w.?]+)(\[\])+', type_str)
        if array_match:
            name = array_match.group(1)
            dimensions = type_str.count('[]')
            return TypeDescriptor(
                name=name.split('.')[-1],
                qualifier='.'.join(name.split('.')[:-1]) if '.' in name else None,
                dimensions=dimensions,
            )
        
        # Simple type
        parts = type_str.replace('?', '').split('.')
        return TypeDescriptor(
            name=parts[-1],
            qualifier='.'.join(parts[:-1]) if len(parts) > 1 else None,
        )

    def _split_generic_args(self, args_str: str) -> List[str]:
        result = []
        depth = 0
        current = ""
        for char in args_str:
            if char == '<':
                depth += 1
                current += char
            elif char == '>':
                depth -= 1
                current += char
            elif char == ',' and depth == 0:
                result.append(current.strip())
                current = ""
            else:
                current += char
        if current.strip():
            result.append(current.strip())
        return result

    def _parse_fields(self, body: str, kind: str) -> List[FieldModel]:
        fields: List[FieldModel] = []
        
        # Parse fields
        for match in self.FIELD_PATTERN.finditer(body):
            modifiers_str = match.group(1) or ""
            modifiers = set(modifiers_str.split())
            type_str = match.group(2).strip()
            name = match.group(3)
            initializer = match.group(4) or ""
            
            # Detect if field is instantiated with 'new'
            is_instantiated = bool(re.search(r'\bnew\b', initializer))
            
            fields.append(FieldModel(
                name=name,
                type_descriptor=self._parse_type_reference(type_str),
                modifiers=modifiers,
                is_instantiated=is_instantiated,
            ))
        
        # Parse properties as fields
        for match in self.PROPERTY_PATTERN.finditer(body):
            modifiers_str = match.group(1) or ""
            modifiers = set(modifiers_str.split())
            type_str = match.group(2).strip()
            name = match.group(3)
            
            fields.append(FieldModel(
                name=name,
                type_descriptor=self._parse_type_reference(type_str),
                modifiers=modifiers,
            ))
        
        return fields

    def _parse_methods(self, body: str, class_name: str, kind: str) -> List[MethodModel]:
        methods: List[MethodModel] = []
        
        # Parse constructors
        for match in self.CONSTRUCTOR_PATTERN.finditer(body):
            modifiers_str = match.group(1) or ""
            modifiers = set(modifiers_str.split())
            name = match.group(2)
            params_str = match.group(3)
            
            if name == class_name:
                methods.append(MethodModel(
                    name=name,
                    return_type=None,
                    parameters=self._parse_parameters(params_str),
                    modifiers=modifiers,
                    is_constructor=True,
                ))
        
        # Parse methods
        for match in self.METHOD_PATTERN.finditer(body):
            modifiers_str = match.group(1) or ""
            modifiers = set(modifiers_str.split())
            return_type_str = match.group(2).strip()
            name = match.group(3)
            params_str = match.group(4)
            
            # Skip if this looks like a constructor
            if name == class_name:
                continue
            
            methods.append(MethodModel(
                name=name,
                return_type=self._parse_type_reference(return_type_str) if return_type_str != "void" else None,
                parameters=self._parse_parameters(params_str),
                modifiers=modifiers,
                is_constructor=False,
            ))
        
        return methods

    def _parse_parameters(self, params_str: str) -> List[MethodParameter]:
        params: List[MethodParameter] = []
        if not params_str.strip():
            return params
        
        # Use _split_generic_args to properly handle generics with commas
        for param in self._split_generic_args(params_str):
            param = param.strip()
            if not param:
                continue
            # Remove modifiers like 'ref', 'out', 'params', 'this'
            param = re.sub(r'\b(ref|out|params|this|in)\b\s*', '', param).strip()
            parts = param.rsplit(None, 1)
            if len(parts) == 2:
                type_str, name = parts
                params.append(MethodParameter(
                    name=name,
                    type_descriptor=self._parse_type_reference(type_str),
                ))
            elif len(parts) == 1:
                params.append(MethodParameter(
                    name=parts[0],
                    type_descriptor=None,
                ))
        return params

    def _parse_enum_constants(self, body: str) -> List[str]:
        constants: List[str] = []
        for match in self.ENUM_VALUE_PATTERN.finditer(body):
            constants.append(match.group(1))
        return constants


class CSharpImportController(BaseImportController):
    """Public entry point used by the UI to import C# code."""

    def __init__(self, application=None, addon_identifier: str = INFJAVA_UML_ADDON_ID,
                 template_id: str = DEFAULT_TEMPLATE_ID):
        super().__init__(application, addon_identifier, template_id)
        self._parser = CSharpSourceParser()

    def import_directory(self, path: str, project_name: Optional[str] = None,
                         view: ImportView = ImportView.INTERNAL) -> ImportReport:
        normalized = os.path.abspath(path)
        if not os.path.exists(normalized):
            raise CSharpImportError(f"Path does not exist: {normalized}")

        cs_files = self._collect_files(normalized, ".cs")
        if not cs_files:
            raise CSharpImportError("No C# files found in the specified directory")

        parse_result = self._parser.parse_files(cs_files)
        if not parse_result.types:
            raise CSharpImportError("No C# types could be parsed from the files")

        name = project_name or os.path.basename(normalized) or "Imported C# Project"
        project = self._create_project(name)

        builder = BaseModelBuilder(project, self._application.ruler, view=view)
        summary = builder.build(parse_result.types)
        if summary.primary_diagram is not None:
            self._application.tabs.select_tab(summary.primary_diagram)
        return ImportReport(summary=summary, warnings=parse_result.errors)
