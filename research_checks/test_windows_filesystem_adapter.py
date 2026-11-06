import ctypes

import pytest

from tests import windows_filesystem_adapter_model as model

ROOT = "C:\\"
STAGE_DIR = "C:\\Stage"
MUSIC_DIR = "C:\\Music"
SOURCE = "C:\\Stage\\song.part"
TARGET = "C:\\Music\\Zoë 🎵.m4a"


class FakeApi:
    def __init__(self):
        self.infos = {
            ROOT: model.HandleInfo(model._ATTR_DIRECTORY, 41, 1, 0, 1),
            STAGE_DIR: model.HandleInfo(model._ATTR_DIRECTORY, 41, 1, 0, 2),
            MUSIC_DIR: model.HandleInfo(model._ATTR_DIRECTORY, 41, 1, 0, 3),
            SOURCE: model.HandleInfo(0, 41, 1, 0, 4),
        }
        self.canonical = {path: "\\\\?\\" + path for path in self.infos}
        self.handles = {}
        self.closed = []
        self.calls = []
        self.entries = {SOURCE: b"staged audio"}
        self.volume_result = (ROOT, model._DRIVE_FIXED, "NTFS")
        self.fail_on = None

    def _call(self, operation):
        self.calls.append(operation)
        if self.fail_on == operation:
            raise model.AdapterError("injected " + operation)

    def volume(self, root):
        self._call("volume")
        return self.volume_result

    def open(self, path, *, directory, writable):
        self._call("open:" + path)
        if path not in self.infos:
            raise model.AdapterError("not found")
        handle = len(self.handles) + 1
        self.handles[handle] = path
        return handle

    def info(self, handle):
        self._call("info:" + self.handles[handle])
        return self.infos[self.handles[handle]]

    def final_path(self, handle):
        self._call("final:" + self.handles[handle])
        return self.canonical[self.handles[handle]]

    def flush(self, handle):
        self._call("flush")

    def rename_no_replace(self, handle, destination):
        self._call("rename")
        if destination in self.entries:
            raise model.AdapterError("already exists")
        self.entries[destination] = self.entries.pop(self.handles[handle])

    def close(self, handle):
        self.closed.append(handle)


def publish(api, source=SOURCE, target=TARGET):
    model.Adapter(api, archive_lock_held=True).publish(
        source, target, archive_lock_held=True, staged_file_owned=True
    )


def test_unicode_path_flushes_before_no_replace_and_closes_handles():
    api = FakeApi()
    publish(api)
    assert api.entries[TARGET] == b"staged audio" and SOURCE not in api.entries
    assert api.calls.index("flush") < api.calls.index("rename")
    assert sorted(api.closed) == sorted(api.handles)


def test_destination_collision_never_overwrites_or_removes_source():
    api = FakeApi()
    api.entries[TARGET] = b"user file"
    with pytest.raises(model.AdapterError, match="already exists"):
        publish(api)
    assert api.entries[TARGET] == b"user file"
    assert api.entries[SOURCE] == b"staged audio"
    assert sorted(api.closed) == sorted(api.handles)


def test_lock_and_staging_ownership_must_be_explicit():
    api = FakeApi()
    with pytest.raises(model.AdapterError, match="archive lock"):
        model.Adapter(api)
    adapter = model.Adapter(api, archive_lock_held=True)
    with pytest.raises(model.AdapterError, match="archive lock"):
        adapter.publish(SOURCE, TARGET)
    with pytest.raises(model.AdapterError, match="ownership"):
        adapter.publish(SOURCE, TARGET, archive_lock_held=True)
    assert not api.calls


@pytest.mark.parametrize(
    "path",
    [
        r"\\server\share\file.m4a",
        r"\\?\C:\stage\file.m4a",
        r"\\.\C:\file.m4a",
        r"C:file.m4a",
        r"c:\file.m4a",
        r"C:\a\..\file.m4a",
        r"C:\a\.\file.m4a",
        r"C:\a\\file.m4a",
        r"C:\a\file.m4a:stream",
        r"C:\a\NUL.m4a",
        r"C:\a\NUL .txt",
        r"C:\a\con  .wav",
        r"C:\a\LPT¹ .m4a",
        r"C:\a\song .m4a ",
        r"C:\a\bad*.m4a",
        "C:\\a\\bad\x00.m4a",
        "C:\\a\\bad\ud800.m4a",
        "C:/a/file.m4a",
        "C:\\a\\COM¹.m4a",
        "C:\\a\\LPT³.txt",
        "C:\\a\\CONIN$",
        "C:\\a\\bad\u202ename.m4a",
        "C:\\" + "x" * 241,
    ],
)
def test_unsafe_remote_alias_device_or_unnormalized_paths_are_rejected(path):
    with pytest.raises(model.AdapterError):
        model._local_path(path)


@pytest.mark.parametrize(
    "volume",
    [
        (ROOT, 4, "NTFS"),
        (ROOT, 3, "ReFS"),
        ("C:\\mount\\", 3, "NTFS"),
        (ROOT, 0, "NTFS"),
        (ROOT, 3, "ntfs"),
    ],
)
def test_only_canonical_fixed_ntfs_volume_is_allowed(volume):
    api = FakeApi()
    api.volume_result = volume
    with pytest.raises(model.AdapterError, match="fixed NTFS"):
        publish(api)
    assert "rename" not in api.calls


@pytest.mark.parametrize("path", [ROOT, STAGE_DIR, MUSIC_DIR, SOURCE])
def test_reparse_point_in_any_component_is_rejected(path):
    api = FakeApi()
    old = api.infos[path]
    api.infos[path] = model.HandleInfo(old.attributes | model._ATTR_REPARSE, 41, 1, 0, 9)
    with pytest.raises(model.AdapterError, match="Reparse"):
        publish(api)
    assert "rename" not in api.calls
    assert sorted(api.closed) == sorted(api.handles)


def test_short_name_or_alias_is_rejected():
    api = FakeApi()
    api.canonical[STAGE_DIR] = "\\\\?\\C:\\Different long name"
    with pytest.raises(model.AdapterError, match="alias"):
        publish(api)
    assert "rename" not in api.calls


def test_hardlinked_source_is_rejected():
    api = FakeApi()
    api.infos[SOURCE] = model.HandleInfo(0, 41, 2, 0, 4)
    with pytest.raises(model.AdapterError, match="Hardlinked"):
        publish(api)
    assert "rename" not in api.calls


def test_cross_volume_child_is_rejected():
    api = FakeApi()
    api.infos[MUSIC_DIR] = model.HandleInfo(model._ATTR_DIRECTORY, 99, 1, 0, 3)
    with pytest.raises(model.AdapterError, match="cross-volume"):
        publish(api)
    assert "rename" not in api.calls


@pytest.mark.parametrize(
    "attribute",
    [model._ATTR_OFFLINE, model._ATTR_RECALL_ON_OPEN, model._ATTR_RECALL_ON_DATA_ACCESS],
)
def test_offline_or_recall_source_is_rejected(attribute):
    api = FakeApi()
    api.infos[SOURCE] = model.HandleInfo(attribute, 41, 1, 0, 4)
    with pytest.raises(model.AdapterError, match="offline"):
        publish(api)
    assert "rename" not in api.calls


@pytest.mark.parametrize("operation", ["volume", "open:C:\\\\", "flush", "rename"])
def test_syscall_errors_abort_and_preserve_source(operation):
    api = FakeApi()
    api.fail_on = operation.replace("C:\\\\", ROOT)
    with pytest.raises(model.AdapterError, match="injected"):
        publish(api)
    assert api.entries[SOURCE] == b"staged audio" and TARGET not in api.entries
    assert sorted(api.closed) == sorted(api.handles)


class Function:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


class MockDll:
    def __init__(self):
        self.rename = None
        self.fail = set()
        self.CreateFileW = Function(
            lambda *_args: model._INVALID_HANDLE if "CreateFileW" in self.fail else 123
        )

        def info(_handle, pointer):
            if "GetFileInformationByHandle" in self.fail:
                return 0
            result = ctypes.cast(pointer, ctypes.POINTER(model._ByHandleInfo)).contents
            result.attributes, result.volume_serial, result.links = 0, 41, 1
            result.index_high, result.index_low = 0, 4
            return 1

        self.GetFileInformationByHandle = Function(info)

        def final(_handle, output, length, _flags):
            path = "\\\\?\\" + SOURCE
            if "GetFinalPathNameByHandleW" in self.fail:
                return 0
            if output is None:
                return len(path) + 1
            output.value = path
            return len(path)

        self.GetFinalPathNameByHandleW = Function(final)

        def volume(_root, output, _length):
            if "GetVolumePathNameW" in self.fail:
                return 0
            output.value = ROOT
            return 1

        self.GetVolumePathNameW = Function(volume)
        self.GetDriveTypeW = Function(lambda _root: model._DRIVE_FIXED)

        def volume_info(_root, _name, _n, _serial, _max, _flags, fs, _size):
            if "GetVolumeInformationW" in self.fail:
                return 0
            fs.value = "NTFS"
            return 1

        self.GetVolumeInformationW = Function(volume_info)
        self.FlushFileBuffers = Function(
            lambda _handle: 0 if "FlushFileBuffers" in self.fail else 1
        )

        def rename(_handle, kind, data, size):
            header = ctypes.cast(data, ctypes.POINTER(model._RenameInfo)).contents
            name = ctypes.string_at(
                ctypes.addressof(data) + model._RenameInfo.filename.offset, header.filename_length
            ).decode("utf-16-le")
            terminator = ctypes.string_at(
                ctypes.addressof(data) + model._RenameInfo.filename.offset + header.filename_length,
                2,
            )
            self.rename = (
                kind,
                header.replace_if_exists,
                header.root_directory,
                name,
                size,
                terminator,
            )
            return 0 if "SetFileInformationByHandle" in self.fail else 1

        self.SetFileInformationByHandle = Function(rename)
        self.CloseHandle = Function(lambda _handle: 0 if "CloseHandle" in self.fail else 1)


def test_native_bindings_fixed_width_and_rename_structure_preserves_unicode():
    dll = MockDll()
    api = model.NativeWin32(dll=dll, error_reader=lambda: 183)
    assert ctypes.sizeof(model.DWORD) == 4
    assert ctypes.sizeof(model.BOOL) == 4
    assert ctypes.sizeof(model._ByHandleInfo) == 52
    assert dll.CreateFileW.restype is model.HANDLE
    assert dll.CreateFileW.argtypes == [
        model.WCHARP,
        model.DWORD,
        model.DWORD,
        ctypes.c_void_p,
        model.DWORD,
        model.DWORD,
        model.HANDLE,
    ]
    assert api.volume(ROOT) == (ROOT, model._DRIVE_FIXED, "NTFS")
    handle = api.open(SOURCE, directory=False, writable=True)
    assert api.info(handle).links == 1
    assert api.final_path(handle) == "\\\\?\\" + SOURCE
    api.flush(handle)
    api.rename_no_replace(handle, TARGET)
    assert dll.rename[0] == model._FILE_RENAME_INFO
    assert dll.rename[1] == 0  # ReplaceIfExists is FALSE.
    assert dll.rename[2] is None
    assert dll.rename[3] == TARGET
    assert dll.rename[4] == model._RenameInfo.filename.offset + len(TARGET.encode("utf-16-le")) + 2
    assert dll.rename[5] == b"\x00\x00"
    api.close(handle)


@pytest.mark.parametrize(
    "operation",
    [
        "CreateFileW",
        "GetFileInformationByHandle",
        "GetFinalPathNameByHandleW",
        "GetVolumePathNameW",
        "GetVolumeInformationW",
        "FlushFileBuffers",
        "SetFileInformationByHandle",
        "CloseHandle",
    ],
)
def test_native_win32_errors_are_surfaced(operation):
    dll = MockDll()
    api = model.NativeWin32(dll=dll, error_reader=lambda: 1234)
    dll.fail.add(operation)
    action = {
        "CreateFileW": lambda: api.open(SOURCE, directory=False, writable=True),
        "GetFileInformationByHandle": lambda: api.info(1),
        "GetFinalPathNameByHandleW": lambda: api.final_path(1),
        "GetVolumePathNameW": lambda: api.volume(ROOT),
        "GetVolumeInformationW": lambda: api.volume(ROOT),
        "FlushFileBuffers": lambda: api.flush(1),
        "SetFileInformationByHandle": lambda: api.rename_no_replace(1, TARGET),
        "CloseHandle": lambda: api.close(1),
    }[operation]
    with pytest.raises(model.AdapterError, match="1234"):
        action()


def test_real_native_backend_requires_explicit_opt_in():
    with pytest.raises(model.AdapterError, match="disabled"):
        model.NativeWin32()


def test_close_failure_after_rename_is_an_ambiguous_result():
    class CloseFails(FakeApi):
        def close(self, handle):
            super().close(handle)
            if self.handles[handle] == SOURCE:
                raise model.AdapterError("close failure after publication")

    api = CloseFails()
    with pytest.raises(model.AdapterError, match="close failure"):
        publish(api)
    # The operation already published the file. A caller must reconcile.
    assert api.entries[TARGET] == b"staged audio"
    assert SOURCE not in api.entries


@pytest.mark.parametrize("name", ["NUL .txt", "CON .txt", "LPT1 .txt", "AUX   .wav", "COM¹ .m4a"])
def test_reserved_device_stems_with_spaces_before_extension_are_rejected(name):
    with pytest.raises(model.AdapterError, match="Unsafe"):
        model._local_path("C:\\Music\\" + name)


def test_nonreserved_stem_with_internal_space_is_accepted():
    root, parts = model._local_path("C:\\Music\\Nulla .txt")
    assert root == "C:\\" and parts == ["Music", "Nulla .txt"]


@pytest.mark.parametrize(
    "source,target",
    [
        ("C:\\Stage\\song.part", "C:\\stage\\SONG.PART"),
        ("C:\\Stage\\Straße.part", "C:\\stage\\STRASSE.PART"),
    ],
)
def test_same_casefolded_source_and_destination_are_refused_before_syscalls(source, target):
    api = FakeApi()
    with pytest.raises(model.AdapterError, match="distinct"):
        publish(api, source, target)
    assert not api.calls


def test_local_path_rejects_cpython_reserved_name_corpus():
    import ntpath

    if not hasattr(ntpath, "isreserved"):
        pytest.skip("requires Python 3.13 reference validator")
    stems = ["CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"] + [
        f"{prefix}{digit}" for prefix in ("COM", "LPT") for digit in "123456789¹²³"
    ]
    candidates = [
        name + spacer + extension
        for stem in stems
        for name in (stem, stem.lower())
        for spacer in ("", " ", "  ")
        for extension in ("", ".txt", ".wav.extra")
    ]
    candidates += [f"normal{chr(code)}name" for code in range(32)]
    candidates += [f"normal{char}name" for char in ':*?"<>|/\\']
    candidates += ["normal.", "normal "]
    for name in candidates:
        if ntpath.isreserved(name):
            with pytest.raises(model.AdapterError):
                model._local_path("C:\\safe\\" + name)
