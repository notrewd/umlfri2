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


class CppImportError(ImportError):
    """Raised when the C++ importer cannot complete the requested action."""


class CppSourceParser:
    """Parse C++ source files into in-memory representations using regex-based parsing."""

    # Regular expressions for C++ parsing
    NAMESPACE_PATTERN = re.compile(r'namespace\s+(\w+)\s*\{')
    CLASS_PATTERN = re.compile(
        r'(template\s*<[^>]+>\s*)?'
        r'(class|struct)\s+(\w+)'
        r'(?:\s*:\s*([^{]+))?'
        r'\s*\{'
    )
    ENUM_PATTERN = re.compile(
        r'enum\s+(?:class\s+)?(\w+)'
        r'(?:\s*:\s*\w+)?'
        r'\s*\{'
    )
    MEMBER_PATTERN = re.compile(
        r'(static|virtual|const|mutable|inline|explicit|\s)*\s*'
        r'([\w:<>,\s*&]+?)\s+'
        r'(\w+)\s*(?:=\s*([^;]+))?;'
    )
    METHOD_PATTERN = re.compile(
        r'(static|virtual|const|inline|explicit|override|final|\s)*\s*'
        r'([\w:<>,\s*&]+?)\s+'
        r'(\w+)\s*\(([^)]*)\)'
        r'\s*(const|override|final|=\s*0|=\s*default|=\s*delete)*'
    )
    CONSTRUCTOR_PATTERN = re.compile(
        r'(explicit|\s)*'
        r'(\w+)\s*\(([^)]*)\)'
        r'\s*(?::\s*[^{]+)?'
        r'\s*\{'
    )
    DESTRUCTOR_PATTERN = re.compile(r'(virtual|\s)*~(\w+)\s*\(\s*\)')
    ACCESS_PATTERN = re.compile(r'(public|private|protected)\s*:')
    ENUM_VALUE_PATTERN = re.compile(r'(\w+)\s*(?:=\s*[^,]+)?[,}]')

    CPP_EXTENSIONS = ('.cpp', '.cxx', '.cc', '.c++', '.hpp', '.hxx', '.h', '.hh')

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
                    # Avoid duplicates from header/source pairs
                    if type_model.full_name not in types:
                        types[type_model.full_name] = type_model
            except Exception as exc:
                errors.append(f"Error parsing {path}: {exc}")
                continue

        return ParseResult(types=types, errors=errors)

    def _parse_content(self, content: str, path: str) -> List[TypeModel]:
        result: List[TypeModel] = []
        
        # Remove comments and strings for cleaner parsing
        clean_content = self._remove_comments_and_strings(content)
        
        # Find namespaces
        namespaces = self._find_namespaces(clean_content)
        
        # Find all class/struct declarations
        for match in self.CLASS_PATTERN.finditer(clean_content):
            template_str = match.group(1) or ""
            kind = match.group(2)  # class or struct
            name = match.group(3)
            base_types_str = match.group(4) or ""
            
            # Determine namespace for this class
            namespace = self._get_namespace_at_position(namespaces, match.start())
            
            modifiers: Set[str] = set()
            if template_str:
                modifiers.add("template")
            
            extends: List[TypeDescriptor] = []
            implements: List[TypeDescriptor] = []
            
            if base_types_str:
                for base in base_types_str.split(","):
                    base = base.strip()
                    # Remove access specifier
                    base = re.sub(r'^(public|private|protected)\s+', '', base)
                    if base:
                        descriptor = self._parse_type_reference(base)
                        if descriptor:
                            extends.append(descriptor)
            
            # Extract body of this class
            body_start = match.end() - 1  # -1 to include the opening brace
            body = self._extract_body(clean_content, body_start)
            
            # For fields and methods, only parse class-level content (not nested method bodies)
            class_level_body = self._extract_class_level_content(body)
            
            fields, methods = self._parse_members(class_level_body, name, kind)
            
            result.append(TypeModel(
                name=name,
                package=namespace,
                kind="class" if kind == "class" else "struct",
                modifiers=modifiers,
                fields=fields,
                methods=methods,
                extends=extends,
                implements=implements,
                source_path=path,
            ))
        
        # Find enum declarations
        for match in self.ENUM_PATTERN.finditer(clean_content):
            name = match.group(1)
            namespace = self._get_namespace_at_position(namespaces, match.start())
            
            body_start = match.end() - 1
            body = self._extract_body(clean_content, body_start)
            enum_constants = self._parse_enum_constants(body)
            
            result.append(TypeModel(
                name=name,
                package=namespace,
                kind="enum",
                modifiers=set(),
                fields=[],
                methods=[],
                extends=[],
                implements=[],
                source_path=path,
                enum_constants=enum_constants,
            ))
        
        return result

    def _remove_comments_and_strings(self, content: str) -> str:
        # Remove multi-line comments
        content = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)
        # Remove single-line comments
        content = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
        # Remove string literals
        content = re.sub(r'"(?:[^"\\]|\\.)*"', '""', content)
        content = re.sub(r"'(?:[^'\\]|\\.)*'", "''", content)
        return content

    def _find_namespaces(self, content: str) -> List[tuple]:
        """Return list of (namespace_name, start_pos, end_pos)."""
        namespaces = []
        for match in self.NAMESPACE_PATTERN.finditer(content):
            name = match.group(1)
            start = match.start()
            # Find matching brace
            end = self._find_matching_brace(content, match.end() - 1)
            if end > 0:
                namespaces.append((name, start, end))
        return namespaces

    def _get_namespace_at_position(self, namespaces: List[tuple], pos: int) -> Optional[str]:
        """Get the innermost namespace at a given position."""
        result = None
        for name, start, end in namespaces:
            if start <= pos <= end:
                if result:
                    result = f"{result}.{name}"
                else:
                    result = name
        return result

    def _find_matching_brace(self, content: str, start: int) -> int:
        """Find the position of the matching closing brace."""
        if start >= len(content) or content[start] != '{':
            return -1
        depth = 0
        for i in range(start, len(content)):
            if content[i] == '{':
                depth += 1
            elif content[i] == '}':
                depth -= 1
                if depth == 0:
                    return i
        return -1

    def _extract_body(self, content: str, start: int) -> str:
        """Extract the body between matching braces."""
        end = self._find_matching_brace(content, start)
        if end > start:
            return content[start + 1:end]
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
            # Skip content inside nested braces (method bodies, etc.)
            i += 1
        return ''.join(result)

    def _parse_type_reference(self, type_str: str) -> Optional[TypeDescriptor]:
        type_str = type_str.strip()
        if not type_str:
            return None
        
        # Remove pointer/reference qualifiers for the type name
        clean_type = re.sub(r'[\s*&]+$', '', type_str)
        clean_type = re.sub(r'\bconst\b', '', clean_type).strip()
        
        # Handle template types
        template_match = re.match(r'([\w:]+)<(.+)>', clean_type)
        if template_match:
            name = template_match.group(1)
            args_str = template_match.group(2)
            arguments = [self._parse_type_reference(arg.strip()) for arg in self._split_template_args(args_str)]
            # Handle namespace::Name
            if '::' in name:
                parts = name.split('::')
                return TypeDescriptor(
                    name=parts[-1],
                    qualifier='.'.join(parts[:-1]),
                    arguments=[a for a in arguments if a],
                )
            return TypeDescriptor(
                name=name,
                arguments=[a for a in arguments if a],
            )
        
        # Handle namespace::Name
        if '::' in clean_type:
            parts = clean_type.split('::')
            return TypeDescriptor(
                name=parts[-1],
                qualifier='.'.join(parts[:-1]),
            )
        
        return TypeDescriptor(name=clean_type)

    def _split_template_args(self, args_str: str) -> List[str]:
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

    def _parse_members(self, body: str, class_name: str, kind: str) -> tuple:
        """Parse class members with access tracking."""
        fields: List[FieldModel] = []
        methods: List[MethodModel] = []
        
        # Default access: private for class, public for struct
        current_access = "private" if kind == "class" else "public"
        
        # Split body into sections by access specifiers
        sections = re.split(r'(public|private|protected)\s*:', body)
        
        for i, section in enumerate(sections):
            if section in ('public', 'private', 'protected'):
                current_access = section
                continue
            
            # Parse fields
            for match in self.MEMBER_PATTERN.finditer(section):
                modifiers_str = match.group(1) or ""
                modifiers = set(modifiers_str.split())
                modifiers.add(current_access)
                type_str = match.group(2).strip()
                name = match.group(3)
                initializer = match.group(4) or ""
                
                # Skip if it looks like a method
                if '(' in type_str:
                    continue
                
                # Detect if field is instantiated with 'new' or brace initialization
                is_instantiated = bool(re.search(r'\bnew\b', initializer)) or bool(re.match(r'\s*\{', initializer))
                
                fields.append(FieldModel(
                    name=name,
                    type_descriptor=self._parse_type_reference(type_str),
                    modifiers=modifiers,
                    is_instantiated=is_instantiated,
                ))
            
            # Parse constructors
            for match in self.CONSTRUCTOR_PATTERN.finditer(section):
                modifiers_str = match.group(1) or ""
                modifiers = set(modifiers_str.split())
                modifiers.add(current_access)
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
            
            # Parse destructor
            for match in self.DESTRUCTOR_PATTERN.finditer(section):
                modifiers_str = match.group(1) or ""
                modifiers = set(modifiers_str.split())
                modifiers.add(current_access)
                name = match.group(2)
                
                if name == class_name:
                    methods.append(MethodModel(
                        name=f"~{name}",
                        return_type=None,
                        parameters=[],
                        modifiers=modifiers,
                        is_constructor=False,
                    ))
            
            # Parse methods
            for match in self.METHOD_PATTERN.finditer(section):
                modifiers_str = match.group(1) or ""
                modifiers = set(modifiers_str.split())
                modifiers.add(current_access)
                return_type_str = match.group(2).strip()
                name = match.group(3)
                params_str = match.group(4)
                suffix = match.group(5) or ""
                
                # Skip constructors/destructors
                if name == class_name or name.startswith('~'):
                    continue
                
                if '= 0' in suffix:
                    modifiers.add('abstract')
                
                methods.append(MethodModel(
                    name=name,
                    return_type=self._parse_type_reference(return_type_str) if return_type_str != "void" else None,
                    parameters=self._parse_parameters(params_str),
                    modifiers=modifiers,
                    is_constructor=False,
                ))
        
        return fields, methods

    def _parse_parameters(self, params_str: str) -> List[MethodParameter]:
        params: List[MethodParameter] = []
        if not params_str.strip():
            return params
        
        for param in self._split_template_args(params_str):  # Reuse for comma splitting
            param = param.strip()
            if not param:
                continue
            
            # Remove default values
            param = re.sub(r'\s*=\s*[^,]+$', '', param)
            
            # Handle "Type name" or "Type* name" or "const Type& name"
            match = re.match(r'(.+?)\s+(\w+)\s*$', param)
            if match:
                type_str = match.group(1).strip()
                name = match.group(2)
                params.append(MethodParameter(
                    name=name,
                    type_descriptor=self._parse_type_reference(type_str),
                ))
            else:
                # Just a type with no name
                params.append(MethodParameter(
                    name="",
                    type_descriptor=self._parse_type_reference(param),
                ))
        
        return params

    def _parse_enum_constants(self, body: str) -> List[str]:
        constants: List[str] = []
        for match in self.ENUM_VALUE_PATTERN.finditer(body):
            constants.append(match.group(1))
        return constants


class CppImportController(BaseImportController):
    """Public entry point used by the UI to import C++ code."""

    def __init__(self, application=None, addon_identifier: str = INFJAVA_UML_ADDON_ID,
                 template_id: str = DEFAULT_TEMPLATE_ID):
        super().__init__(application, addon_identifier, template_id)
        self._parser = CppSourceParser()

    def import_directory(self, path: str, project_name: Optional[str] = None,
                         view: ImportView = ImportView.INTERNAL) -> ImportReport:
        normalized = os.path.abspath(path)
        if not os.path.exists(normalized):
            raise CppImportError(f"Path does not exist: {normalized}")

        cpp_files = self._collect_cpp_files(normalized)
        if not cpp_files:
            raise CppImportError("No C++ files found in the specified directory")

        parse_result = self._parser.parse_files(cpp_files)
        if not parse_result.types:
            raise CppImportError("No C++ types could be parsed from the files")

        name = project_name or os.path.basename(normalized) or "Imported C++ Project"
        project = self._create_project(name)

        builder = BaseModelBuilder(project, self._application.ruler, view=view)
        summary = builder.build(parse_result.types)
        if summary.primary_diagram is not None:
            self._application.tabs.select_tab(summary.primary_diagram)
        return ImportReport(summary=summary, warnings=parse_result.errors)

    def _collect_cpp_files(self, path: str) -> List[str]:
        """Collect all C++ source and header files."""
        normalized = os.path.abspath(path)
        extensions = CppSourceParser.CPP_EXTENSIONS
        
        if os.path.isfile(normalized):
            if any(normalized.endswith(ext) for ext in extensions):
                return [normalized]
            return []
        
        files: List[str] = []
        for root, _, filenames in os.walk(normalized):
            for filename in filenames:
                if any(filename.endswith(ext) for ext in extensions):
                    files.append(os.path.join(root, filename))
        return sorted(files)
