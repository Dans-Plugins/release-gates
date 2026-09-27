#!/usr/bin/env python3
"""API-compatibility check: a plugin jar only uses Bukkit API that exists on every Minecraft
version its repository supports.

A plugin is compiled against one spigot-api but runs on every version in its
`minecraft-versions.json`. A reference to a class, field or method that one of those versions
does not have compiles fine and then fails on that server: Medieval Factions 6.0.0 referenced
`PotionType.LONG_POISON`, which only exists from 1.20.5, and threw NoSuchFieldError on enable
on 1.19.4 (Dans-Plugins/Medieval-Factions#2042). A class that changed between enum and
interface (`Attribute` in 1.21.3) fails with IncompatibleClassChangeError instead.

For every supported version, this downloads that version's spigot-api jar and checks each
`org/bukkit` reference in the plugin's own classes the way the JVM resolves it:

  class    the referenced class exists
  field    the field exists on the class, its superinterfaces or its superclasses
  method   the method exists on the class, its superclasses or its superinterfaces
  kind     a method referenced as a class method is on a class, and one referenced as an
           interface method is on an interface
  java     no class in the jar is compiled for a newer Java than the version can run on: the
           oldest Java it supports (16 for 1.17, 17 up to 1.20.4, 21 up to 1.21.x, 25 from
           26.x). A server on that Java cannot load such a class at all (Herald compiled for
           Java 21 failed to load on 1.19.4). Multi-release entries under META-INF/versions/
           are skipped: the JVM only loads the ones it can run.

Every class in the jar that references `org/bukkit` is checked — the plugin's own code, code
it bundles from other plugins, and shaded libraries alike, since all of it runs on the server.
The exception is a library built to span Minecraft versions, which references newer API on
purpose behind its own runtime version checks: XSeries (`com.cryptomorin.xseries`, relocated
or not). Its classes are skipped and the output says how many. `--exclude a/b/` skips another
package the same way; use it only for code that guards every version-specific reference.

The check is deliberately strict in both directions. Newer servers rewrite some renamed
constants at load (`Material.GRASS` → `SHORT_GRASS`), but a plugin that relies on that fails
here: resolve the name at runtime instead, which works on every version.

Usage:
  check_api_compat.py [--versions minecraft-versions.json] [--exclude a/b/] PATH [PATH ...]

A PATH is the plugin jar or a directory holding it (`target`, `build/libs`); directories that
do not exist are skipped, so one command serves Maven and Gradle builds. Exactly one plugin
jar must be found: a jar with a plugin.yml, not `original-*`, `*-sources`, `*-javadoc` or
`*-plain`. Exits 1 and lists every problem, per version, when any reference does not resolve.
Only the standard library is used.
"""

import json
import os
import struct
import sys
import urllib.request
import zipfile

REPOSITORY = "https://hub.spigotmc.org/nexus/content/repositories/snapshots/org/spigotmc/spigot-api"
# Libraries that reference API from several Minecraft versions on purpose, each behind a
# runtime version check; matched anywhere in a class name, so relocated copies count too.
VERSION_GUARDED_LIBRARIES = ("cryptomorin/xseries/",)
API_PACKAGE = "org/bukkit/"
ACC_INTERFACE = 0x0200
CACHE_DIR = os.environ.get("API_COMPAT_CACHE", os.path.join(os.path.expanduser("~"), ".cache", "api-compat"))


# --- class file parsing --------------------------------------------------------------------

class ClassInfo:
    def __init__(self, name, is_interface, super_name, interfaces, fields, methods, refs, class_refs):
        self.name = name
        self.is_interface = is_interface
        self.super_name = super_name
        self.interfaces = interfaces
        self.fields = fields      # {(name, descriptor)}
        self.methods = methods    # {(name, descriptor)}
        self.refs = refs          # [(kind, owner, name, descriptor)]; kind: field | method | imethod
        self.class_refs = class_refs  # {internal class name}


def parse_class(data):
    """The parts of a class file this check needs (JVMS §4)."""
    if data[:4] != b"\xca\xfe\xba\xbe":
        raise ValueError("not a class file")
    pos = 8
    (count,) = struct.unpack_from(">H", data, pos)
    pos += 2
    pool = [None] * count
    i = 1
    while i < count:
        tag = data[pos]
        pos += 1
        if tag == 1:  # Utf8
            (length,) = struct.unpack_from(">H", data, pos)
            pool[i] = ("utf8", data[pos + 2:pos + 2 + length].decode("utf-8", errors="replace"))
            pos += 2 + length
        elif tag in (3, 4):  # Integer, Float
            pos += 4
        elif tag in (5, 6):  # Long, Double take two slots
            pos += 8
            i += 1
        elif tag == 7:  # Class
            pool[i] = ("class", struct.unpack_from(">H", data, pos)[0])
            pos += 2
        elif tag in (8, 16, 19, 20):  # String, MethodType, Module, Package
            pos += 2
        elif tag in (9, 10, 11):  # Fieldref, Methodref, InterfaceMethodref
            pool[i] = ({9: "field", 10: "method", 11: "imethod"}[tag],) + struct.unpack_from(">HH", data, pos)
            pos += 4
        elif tag == 12:  # NameAndType
            pool[i] = ("nat",) + struct.unpack_from(">HH", data, pos)
            pos += 4
        elif tag == 15:  # MethodHandle
            pos += 3
        elif tag in (17, 18):  # Dynamic, InvokeDynamic
            pos += 4
        else:
            raise ValueError(f"unknown constant pool tag {tag}")
        i += 1

    def utf8(index):
        return pool[index][1]

    def class_name(index):
        return utf8(pool[index][1]) if index else None

    access, this_index, super_index, n_interfaces = struct.unpack_from(">HHHH", data, pos)
    pos += 8
    interfaces = [class_name(struct.unpack_from(">H", data, pos + 2 * k)[0]) for k in range(n_interfaces)]
    pos += 2 * n_interfaces

    def members():
        nonlocal pos
        (n,) = struct.unpack_from(">H", data, pos)
        pos += 2
        out = set()
        for _ in range(n):
            _, name_index, desc_index, n_attributes = struct.unpack_from(">HHHH", data, pos)
            pos += 8
            for _ in range(n_attributes):
                (length,) = struct.unpack_from(">I", data, pos + 2)
                pos += 6 + length
            out.add((utf8(name_index), utf8(desc_index)))
        return out

    fields = members()
    methods = members()

    refs = []
    class_refs = set()
    for entry in pool:
        if not entry:
            continue
        if entry[0] in ("field", "method", "imethod"):
            _, nat_name, nat_desc = pool[entry[2]]
            refs.append((entry[0], class_name(entry[1]), utf8(nat_name), utf8(nat_desc)))
        elif entry[0] == "class":
            name = utf8(entry[1])
            # array descriptors ("[Lorg/bukkit/Material;") name their element class
            if name.startswith("["):
                name = name.lstrip("[")
                name = name[1:-1] if name.startswith("L") else None
            if name:
                class_refs.add(name)
    return ClassInfo(class_name(this_index), bool(access & ACC_INTERFACE), class_name(super_index),
                     interfaces, fields, methods, refs, class_refs)


def read_classes(jar_path, prefix, exclude=None):
    out = {}
    with zipfile.ZipFile(jar_path) as jar:
        for entry in jar.namelist():
            if entry.endswith(".class") and entry.startswith(prefix) and not (exclude and entry.startswith(exclude)):
                info = parse_class(jar.read(entry))
                out[info.name] = info
    return out


# --- the API side --------------------------------------------------------------------------

def api_jar(version):
    """The spigot-api jar for `version`, downloaded once into CACHE_DIR."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"spigot-api-{version}.jar")
    if os.path.exists(path):
        return path
    base = f"{REPOSITORY}/{version}-R0.1-SNAPSHOT"
    metadata = urllib.request.urlopen(f"{base}/maven-metadata.xml", timeout=60).read().decode()
    stamp = metadata.split("<value>", 1)[1].split("</value>", 1)[0]
    urllib.request.urlretrieve(f"{base}/spigot-api-{stamp}.jar", path + ".part")
    os.replace(path + ".part", path)
    return path


class Api:
    def __init__(self, classes):
        self.classes = classes

    def _supertypes(self, name):
        info = self.classes.get(name)
        if not info:
            return [], []
        return ([info.super_name] if info.super_name else []), list(info.interfaces)

    def has_field(self, owner, name, desc, seen=None):
        """JVMS §5.4.3.2: the class, then its superinterfaces, then its superclass."""
        seen = seen if seen is not None else set()
        if owner in seen:
            return False
        seen.add(owner)
        info = self.classes.get(owner)
        if info is None:
            # A supertype outside the API (java.lang.Enum, Object) declares no Bukkit field.
            # Treating it as "might have it" is exactly how an enum constant that is not there
            # would slip through.
            return False
        if (name, desc) in info.fields:
            return True
        supers, interfaces = self._supertypes(owner)
        return any(self.has_field(t, name, desc, seen) for t in interfaces + supers)

    def has_method(self, owner, name, desc, seen=None):
        """JVMS §5.4.3.3/§5.4.3.4: the class and its superclasses, then superinterfaces.
        A supertype outside the API (java.lang.Enum, Object, …) is taken to provide the
        method: those are the JDK's `values`-style members, not the API's."""
        seen = seen if seen is not None else set()
        if owner in seen:
            return False
        seen.add(owner)
        info = self.classes.get(owner)
        if info is None:
            return not owner.startswith(API_PACKAGE)
        if (name, desc) in info.methods:
            return True
        supers, interfaces = self._supertypes(owner)
        return any(self.has_method(t, name, desc, seen) for t in supers + interfaces)


def check(plugin, api):
    """Problems as (plugin class, message), sorted and de-duplicated."""
    problems = set()
    for info in plugin.values():
        where = info.name.rsplit("/", 1)[-1]
        for name in info.class_refs:
            if name.startswith(API_PACKAGE) and name not in api.classes:
                problems.add((where, f"class {name} does not exist"))
        for kind, owner, name, desc in info.refs:
            if not owner or not owner.startswith(API_PACKAGE):
                continue
            target = api.classes.get(owner)
            if target is None:
                continue  # reported as a missing class above
            if kind == "field":
                if not api.has_field(owner, name, desc):
                    problems.add((where, f"field {owner}.{name} does not exist"))
                continue
            if kind == "method" and target.is_interface:
                problems.add((where, f"{owner}.{name} is called as a class method, but {owner} is an interface"))
            elif kind == "imethod" and not target.is_interface:
                problems.add((where, f"{owner}.{name} is called as an interface method, but {owner} is a class"))
            elif not api.has_method(owner, name, desc):
                problems.add((where, f"method {owner}.{name}{desc} does not exist"))
    return sorted(problems)


def minimum_java(version):
    """The oldest Java a server of this Minecraft version runs on (the same mapping as
    OMCSI's java-for-minecraft.sh), or None for a version this check does not know."""
    parts = version.split(".")
    if parts[0] == "1" and len(parts) >= 2 and all(p.isdigit() for p in parts[1:]):
        minor = int(parts[1])
        patch = int(parts[2]) if len(parts) > 2 else 0
        if minor < 17:
            return 8
        if minor == 17:
            return 16
        if minor < 20 or (minor == 20 and patch < 5):
            return 17
        return 21
    if parts[0].isdigit() and int(parts[0]) >= 26:
        return 25
    return None


def class_file_levels(jar):
    """{class file major version: count} for every class a JVM would load from `jar`."""
    levels = {}
    with zipfile.ZipFile(jar) as z:
        for entry in z.namelist():
            if entry.endswith(".class") and not entry.startswith("META-INF/"):
                major = struct.unpack(">H", z.read(entry)[6:8])[0]
                levels[major] = levels.get(major, 0) + 1
    return levels


def java_problems(levels, version):
    java = minimum_java(version)
    if java is None:
        return [("(jar)", f"unknown Minecraft version {version}: no Java level to check against")]
    newest = java + 44  # class file major version = Java version + 44
    too_new = {m: n for m, n in levels.items() if m > newest}
    return [("(jar)", f"{n} class(es) compiled for Java {m - 44}; Minecraft {version} runs on Java {java}")
            for m, n in sorted(too_new.items())]


def find_plugin_jar(paths):
    """The one plugin jar among `paths` (jars, or directories holding them)."""
    candidates = []
    for path in paths:
        if os.path.isdir(path):
            candidates += [os.path.join(path, n) for n in sorted(os.listdir(path)) if n.endswith(".jar")]
        elif os.path.isfile(path):
            candidates.append(path)
    jars = []
    for jar in candidates:
        name = os.path.basename(jar)
        if name.startswith("original-") or any(name.endswith(x) for x in ("-sources.jar", "-javadoc.jar", "-plain.jar")):
            continue
        with zipfile.ZipFile(jar) as z:
            if "plugin.yml" in z.namelist():
                jars.append(jar)
    if len(jars) != 1:
        raise SystemExit(f"expected exactly one plugin jar in {paths}, found {len(jars) or 'none'}: {jars}")
    return jars[0]


def classes_to_check(jar, extra_excludes):
    """Every class in `jar` that references Bukkit, minus version-guarded libraries and
    `extra_excludes`. Returns (checked, skipped count)."""
    users = {n: c for n, c in read_classes(jar, "").items()
             if any(r.startswith(API_PACKAGE) for r in c.class_refs)}
    checked = {n: c for n, c in users.items()
               if not any(lib in n + "/" for lib in VERSION_GUARDED_LIBRARIES)
               and not (n + "/").startswith(tuple(extra_excludes))}
    return checked, len(users) - len(checked)


def main(argv):
    args = argv[1:]
    versions_file = "minecraft-versions.json"
    excludes = []
    paths = []
    while args:
        arg = args.pop(0)
        if arg == "--versions" and args:
            versions_file = args.pop(0)
        elif arg == "--exclude" and args:
            excludes.append(args.pop(0).replace(".", "/").rstrip("/") + "/")
        elif arg in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            paths.append(arg)
    if not paths:
        print(__doc__)
        return 2
    with open(versions_file) as f:
        versions = json.load(f)["supported"]
    jar = find_plugin_jar(paths)
    plugin, skipped = classes_to_check(jar, excludes)
    print(f"{os.path.basename(jar)}: {len(plugin)} classes use Bukkit"
          + (f" ({skipped} in version-guarded libraries skipped)" if skipped else "")
          + f"; supported Minecraft versions: {', '.join(versions)}")
    if not plugin:
        print("no class references Bukkit — nothing was checked")
        return 1
    levels = class_file_levels(jar)
    failed = False
    for version in versions:
        api = Api(read_classes(api_jar(version), API_PACKAGE))
        problems = java_problems(levels, version) + check(plugin, api)
        if problems:
            failed = True
            print(f"\n{version}: {len(problems)} problem(s)")
            for where, message in problems:
                print(f"  {where}: {message}")
        else:
            print(f"{version}: every Bukkit reference resolves; bytecode fits Java {minimum_java(version)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
