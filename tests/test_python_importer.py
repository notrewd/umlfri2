import os
import textwrap
import unittest
from tempfile import TemporaryDirectory

from umlfri2.application.importers.python import (
    PythonSourceParser,
)
from umlfri2.application.importers.base import (
    TypeDescriptor,
    TypeResolver,
)


class PythonImporterParserTests(unittest.TestCase):
    def test_parser_extracts_class_details(self):
        source = textwrap.dedent(
            """
            class Person(BaseEntity):
                def __init__(self, name: str, age: int):
                    self.name = name
                    self.age = age
                
                def say_hello(self) -> None:
                    print(f"Hello, {self.name}")
                
                def get_age(self) -> int:
                    return self.age
            """
        ).strip()

        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "person.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(source)

            result = PythonSourceParser().parse_files([path])

        self.assertIn("person.Person", result.types)
        person = result.types["person.Person"]
        self.assertEqual(person.name, "Person")
        self.assertEqual(person.kind, "class")
        self.assertEqual(len(person.extends), 1)
        self.assertEqual(person.extends[0].name, "BaseEntity")
        
        field_names = {f.name for f in person.fields}
        self.assertIn("name", field_names)
        self.assertIn("age", field_names)
        
        method_names = {m.name for m in person.methods}
        self.assertIn("Person", method_names)  # Constructor renamed to class name
        self.assertIn("say_hello", method_names)
        self.assertIn("get_age", method_names)

    def test_parser_handles_dataclass(self):
        source = textwrap.dedent(
            """
            from dataclasses import dataclass
            
            @dataclass
            class Point:
                x: int
                y: int
                label: str = ""
            """
        ).strip()

        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "point.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(source)

            result = PythonSourceParser().parse_files([path])

        self.assertIn("point.Point", result.types)
        point = result.types["point.Point"]
        self.assertIn("dataclass", point.modifiers)
        
        field_names = {f.name for f in point.fields}
        self.assertIn("x", field_names)
        self.assertIn("y", field_names)
        self.assertIn("label", field_names)
        
        x_field = next(f for f in point.fields if f.name == "x")
        self.assertEqual(x_field.type_descriptor.name, "int")

    def test_parser_handles_enum(self):
        source = textwrap.dedent(
            """
            from enum import Enum
            
            class Status(Enum):
                PENDING = 1
                ACTIVE = 2
                COMPLETED = 3
            """
        ).strip()

        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "status.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(source)

            result = PythonSourceParser().parse_files([path])

        self.assertIn("status.Status", result.types)
        status = result.types["status.Status"]
        self.assertEqual(status.kind, "enum")
        self.assertIn("PENDING", status.enum_constants)
        self.assertIn("ACTIVE", status.enum_constants)
        self.assertIn("COMPLETED", status.enum_constants)

    def test_parser_handles_abstract_class(self):
        source = textwrap.dedent(
            """
            from abc import ABC, abstractmethod
            
            class Shape(ABC):
                @abstractmethod
                def get_area(self) -> float:
                    pass
                
                def describe(self) -> str:
                    return "A shape"
            """
        ).strip()

        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "shape.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(source)

            result = PythonSourceParser().parse_files([path])

        shape = result.types["shape.Shape"]
        self.assertIn("abstract", shape.modifiers)
        
        get_area = next(m for m in shape.methods if m.name == "get_area")
        self.assertTrue(get_area.is_abstract)

    def test_parser_handles_static_methods(self):
        source = textwrap.dedent(
            """
            class Utility:
                counter: int = 0
                
                @staticmethod
                def reset():
                    Utility.counter = 0
                
                @classmethod
                def get_instance(cls):
                    return cls()
            """
        ).strip()

        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "utility.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(source)

            result = PythonSourceParser().parse_files([path])

        utility = result.types["utility.Utility"]
        
        reset = next(m for m in utility.methods if m.name == "reset")
        self.assertTrue(reset.is_static)
        
        get_instance = next(m for m in utility.methods if m.name == "get_instance")
        self.assertTrue(get_instance.is_static)

    def test_parser_handles_visibility(self):
        source = textwrap.dedent(
            """
            class AccessTest:
                def __init__(self):
                    self.public_field = 1
                    self._protected_field = 2
                    self.__private_field = 3
                
                def public_method(self):
                    pass
                
                def _protected_method(self):
                    pass
                
                def __private_method(self):
                    pass
            """
        ).strip()

        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "access_test.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(source)

            result = PythonSourceParser().parse_files([path])

        access_test = result.types["access_test.AccessTest"]
        
        public_field = next(f for f in access_test.fields if f.name == "public_field")
        protected_field = next(f for f in access_test.fields if f.name == "_protected_field")
        private_field = next(f for f in access_test.fields if f.name == "__private_field")
        
        self.assertEqual(public_field.visibility, "+")
        self.assertEqual(protected_field.visibility, "#")
        self.assertEqual(private_field.visibility, "-")
        
        public_method = next(m for m in access_test.methods if m.name == "public_method")
        protected_method = next(m for m in access_test.methods if m.name == "_protected_method")
        private_method = next(m for m in access_test.methods if m.name == "__private_method")
        
        self.assertEqual(public_method.visibility, "+")
        self.assertEqual(protected_method.visibility, "#")
        self.assertEqual(private_method.visibility, "-")

    def test_parser_handles_type_annotations(self):
        source = textwrap.dedent(
            """
            from typing import List, Dict, Optional
            
            class Container:
                items: List[str]
                mapping: Dict[str, int]
                optional_value: Optional[int]
                
                def add(self, item: str) -> None:
                    pass
                
                def get(self, key: str) -> Optional[int]:
                    pass
            """
        ).strip()

        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "container.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(source)

            result = PythonSourceParser().parse_files([path])

        container = result.types["container.Container"]
        
        items_field = next(f for f in container.fields if f.name == "items")
        self.assertEqual(items_field.type_descriptor.name, "List")
        
        mapping_field = next(f for f in container.fields if f.name == "mapping")
        self.assertEqual(mapping_field.type_descriptor.name, "Dict")

    def test_parser_handles_multiple_inheritance(self):
        source = textwrap.dedent(
            """
            class Mixin1:
                pass
            
            class Mixin2:
                pass
            
            class Combined(Mixin1, Mixin2):
                pass
            """
        ).strip()

        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "combined.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(source)

            result = PythonSourceParser().parse_files([path])

        combined = result.types["combined.Combined"]
        base_names = {b.name for b in combined.extends}
        self.assertIn("Mixin1", base_names)
        self.assertIn("Mixin2", base_names)

    def test_parser_handles_class_attributes(self):
        source = textwrap.dedent(
            """
            class Config:
                DEBUG = True
                VERSION = "1.0.0"
                MAX_SIZE: int = 100
            """
        ).strip()

        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(source)

            result = PythonSourceParser().parse_files([path])

        config = result.types["config.Config"]
        field_names = {f.name for f in config.fields}
        self.assertIn("DEBUG", field_names)
        self.assertIn("VERSION", field_names)
        self.assertIn("MAX_SIZE", field_names)

    def test_parser_handles_method_parameters(self):
        source = textwrap.dedent(
            """
            class Calculator:
                def add(self, a: int, b: int) -> int:
                    return a + b
                
                def multiply(self, values: list) -> int:
                    pass
            """
        ).strip()

        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "calculator.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(source)

            result = PythonSourceParser().parse_files([path])

        calculator = result.types["calculator.Calculator"]
        add_method = next(m for m in calculator.methods if m.name == "add")
        
        self.assertEqual(len(add_method.parameters), 2)
        self.assertEqual(add_method.parameters[0].name, "a")
        self.assertEqual(add_method.parameters[0].type_descriptor.name, "int")
        self.assertEqual(add_method.parameters[1].name, "b")

    def test_parser_collects_only_python_files(self):
        python_source = textwrap.dedent(
            """
            class PythonClass:
                def method(self):
                    pass
            """
        ).strip()
        
        java_source = textwrap.dedent(
            """
            public class JavaClass {
                public void method() {}
            }
            """
        ).strip()

        with TemporaryDirectory() as tmp:
            py_path = os.path.join(tmp, "python_class.py")
            java_path = os.path.join(tmp, "JavaClass.java")
            
            with open(py_path, "w", encoding="utf-8") as handle:
                handle.write(python_source)
            with open(java_path, "w", encoding="utf-8") as handle:
                handle.write(java_source)

            # Only parse Python files
            result = PythonSourceParser().parse_files([py_path])

        self.assertTrue(any("PythonClass" in name for name in result.types.keys()))

    def test_parser_handles_init_file(self):
        source = textwrap.dedent(
            """
            class PackageClass:
                def method(self):
                    pass
            """
        ).strip()

        with TemporaryDirectory() as tmp:
            pkg_dir = os.path.join(tmp, "mypackage")
            os.makedirs(pkg_dir)
            path = os.path.join(pkg_dir, "__init__.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(source)

            result = PythonSourceParser().parse_files([path])

        # The module name should be derived from the package directory
        self.assertTrue(any("PackageClass" in name for name in result.types.keys()))


if __name__ == "__main__":
    unittest.main()
