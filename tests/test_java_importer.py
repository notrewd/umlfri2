import os
import textwrap
import unittest
from tempfile import TemporaryDirectory

from umlfri2.application.importers.java import (
    ImportContext,
    JavaSourceParser,
    JavaTypeModel,
    JavaTypeResolver,
    TypeDescriptor,
)


class JavaImporterParserTests(unittest.TestCase):
    def test_parser_extracts_class_details(self):
        source = textwrap.dedent(
            """
            package com.example;
            import java.util.List;
            public class Foo extends Base implements Runnable {
                private String value;
                protected static List<Bar> bars;

                public Foo(String value) {}

                @Override
                public void run() {}
            }
            """
        ).strip()

        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "Foo.java")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(source)

            result = JavaSourceParser().parse_files([path])

        self.assertIn("com.example.Foo", result.types)
        foo = result.types["com.example.Foo"]
        self.assertEqual(foo.extends[0].name, "Base")
        self.assertEqual(foo.implements[0].name, "Runnable")
        self.assertEqual(len(foo.fields), 2)
        self.assertEqual(foo.fields[0].name, "value")
        self.assertEqual(foo.fields[0].type_descriptor.name, "String")
        self.assertEqual(foo.fields[1].type_descriptor.name, "List")
        self.assertEqual(foo.fields[1].type_descriptor.arguments[0].name, "Bar")
        method_names = {method.name for method in foo.methods}
        self.assertIn("Foo", method_names)
        self.assertIn("run", method_names)

    def test_parser_handles_basic_types(self):
        source = textwrap.dedent(
            """
            public class WithPrimitives {
                private int counter;
                protected double[] measurements;
            }
            """
        ).strip()

        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "WithPrimitives.java")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(source)

            result = JavaSourceParser().parse_files([path])

        model = result.types["WithPrimitives"]
        primitive_field = next(field for field in model.fields if field.name == "counter")
        array_field = next(field for field in model.fields if field.name == "measurements")
        self.assertEqual(primitive_field.type_descriptor.name, "int")
        self.assertEqual(array_field.type_descriptor.name, "double")
        self.assertEqual(array_field.type_descriptor.dimensions, 1)

    def test_resolver_prefers_imports(self):
        target_primary = JavaTypeModel(
            name="Target",
            package="com.sample",
            kind="class",
            modifiers=set(),
            fields=[],
            methods=[],
            extends=[],
            implements=[],
            imports=ImportContext(),
            source_path="/tmp/Target.java",
        )
        target_secondary = JavaTypeModel(
            name="Target",
            package="org.alt",
            kind="class",
            modifiers=set(),
            fields=[],
            methods=[],
            extends=[],
            implements=[],
            imports=ImportContext(),
            source_path="/tmp/Target2.java",
        )
        source_model = JavaTypeModel(
            name="Source",
            package="com.example",
            kind="class",
            modifiers=set(),
            fields=[],
            methods=[],
            extends=[],
            implements=[],
            imports=ImportContext(direct_imports={"Target": "com.sample.Target"}),
            source_path="/tmp/Source.java",
        )

        models = {
            target_primary.full_name: target_primary,
            target_secondary.full_name: target_secondary,
            source_model.full_name: source_model,
        }

        resolver = JavaTypeResolver(models)
        descriptor = TypeDescriptor(name="Target")
        resolved = resolver.resolve(descriptor, source_model)
        self.assertEqual(resolved, "com.sample.Target")


if __name__ == "__main__":
    unittest.main()
