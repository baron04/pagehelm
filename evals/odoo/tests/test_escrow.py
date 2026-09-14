import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from evals.odoo import escrow
from evals.odoo.revision import REPOSITORY

CONTENT = {
    "heldout/p74-odoo-heldout-v1.json": b'{"set_id": "synthetic-heldout", "tasks": []}\n',
    "seeds/development.seed": b"00112233445566778899aabbccddeeff\n",
    "seeds/heldout.seed": b"ffeeddccbbaa99887766554433221100\n",
}
INTERNAL = {"media": "APPLE SSD", "registry": "APPLE SSD Media", "bus": "Apple Fabric",
            "size": 1000, "device_tree": "IODeviceTree:/ans"}
EXTERNAL = {"media": "Portable SSD", "registry": "Portable SSD Media", "bus": "USB",
            "size": 2000, "device_tree": "IOService:/usb"}


def volume(disks, *, fstype="apfs", source="/dev/disk3s5", virtual=False):
    return {"source": source, "mount": "/", "fstype": fstype, "disks": disks, "virtual": virtual}


def separate_domains(path):
    """Operating-system facts for a synthetic layout: the archive on a network share, the secret
    on an external disk and everything else on the internal disk."""
    parts = Path(path).parts
    if "vault" in parts:
        return volume([], fstype="smbfs", source="//owner@nas.example.test/escrow")
    if "keys" in parts:
        return volume([EXTERNAL], source="/dev/disk8s1")
    return volume([INTERNAL])


@unittest.skipIf(shutil.which("openssl") is None, "openssl is not installed")
class EscrowTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.private = self.root / "private"
        for name, data in CONTENT.items():
            (self.private / name).parent.mkdir(parents=True, exist_ok=True)
            (self.private / name).write_bytes(data)
        self.archive = self.root / "vault" / "p74.escrow"
        self.secret = self.root / "keys" / "p74.secret"

    def create(self):
        return escrow.create(self.private, self.archive, self.secret)

    def test_a_created_escrow_is_encrypted_and_matches_the_private_files(self):
        record = self.create()
        self.assertEqual((record["status"], record["files"], record["matches_private_files"]),
                         ("READY", 3, True))
        data = self.archive.read_bytes()
        for content in CONTENT.values():
            self.assertNotIn(content.strip(), data)
        self.assertEqual(os.stat(self.archive).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.secret).st_mode & 0o777, 0o600)

    def test_restore_recovers_every_file_into_a_new_directory_only(self):
        self.create()
        destination = self.root / "recovered"
        record = escrow.restore(self.archive, self.secret, destination)
        self.assertTrue(record["matches_private_files"])
        for name, data in CONTENT.items():
            self.assertEqual((destination / name).read_bytes(), data)
        with self.assertRaises(escrow.EscrowError):
            escrow.restore(self.archive, self.secret, destination)

    def test_an_altered_archive_a_wrong_secret_or_changed_sources_are_refused(self):
        self.create()
        other = self.root / "other-keys" / "other.secret"
        other.parent.mkdir()
        other.write_text(os.urandom(32).hex() + "\n")
        with self.assertRaises(escrow.EscrowError):
            escrow.verify(self.archive, other)
        data = bytearray(self.archive.read_bytes())
        data[len(data) // 2] ^= 0x01
        altered = self.root / "vault" / "altered.escrow"
        altered.write_bytes(bytes(data))
        with self.assertRaises(escrow.EscrowError):
            escrow.verify(altered, self.secret)
        (self.private / "seeds" / "heldout.seed").write_bytes(b"changed\n")
        with self.assertRaises(escrow.EscrowError):
            escrow.verify(self.archive, self.secret, private_dir=self.private)
        self.assertTrue(escrow.verify(self.archive, self.secret)["status"] == "READY")

    def test_placement_rules_are_enforced_before_anything_is_written(self):
        self.create()
        with self.assertRaises(escrow.EscrowError):
            self.create()
        with self.assertRaises(escrow.EscrowError):
            escrow.create(self.private, self.root / "same" / "a.escrow",
                          self.root / "same" / "a.secret")
        with self.assertRaises(escrow.EscrowError):
            escrow.create(self.private, REPOSITORY / ".private" / "never.escrow",
                          self.root / "k2" / "never.secret")
        (self.private / "seeds" / "development.seed").unlink()
        with self.assertRaises(escrow.EscrowError):
            escrow.create(self.private, self.root / "v3" / "x.escrow", self.root / "k3" / "x.secret")
        self.assertFalse((REPOSITORY / ".private" / "never.escrow").exists())

    def custody(self, **options):
        return escrow.custody_record(private_dir=self.private, archive=self.archive,
                                     secret=self.secret, clock=lambda: "2026-09-15T08:00:00Z",
                                     **{"probe": separate_domains, **options})

    def test_custody_spans_three_proven_domains_without_paths_or_secrets(self):
        self.create()
        record = self.custody()
        self.assertEqual({name: item["kind"] for name, item in record["domains"].items()},
                         {"original": "physical_device", "archive": "network_volume",
                          "secret": "physical_device"})
        self.assertEqual(record["restore"],
                         {"status": "READY", "files": 3, "matches_private_files": True})
        text = json.dumps(record)
        self.assertNotIn(str(self.root), text)
        self.assertNotIn(self.secret.read_text().strip(), text)
        self.assertEqual(escrow.verify_custody(record, private_dir=self.private), record)
        with self.assertRaises(escrow.EscrowError):
            escrow.verify_custody({**record, "verified_at": "2026-09-16T08:00:00Z"},
                                  private_dir=self.private)
        (self.private / "seeds" / "heldout.seed").write_bytes(b"changed\n")
        with self.assertRaises(escrow.EscrowError):
            escrow.verify_custody(record, private_dir=self.private)

    def test_a_shared_or_unprovable_failure_domain_is_refused(self):
        self.create()

        def probe_with(archive=None, secret=None):
            def probe(path):
                parts = Path(path).parts
                if "vault" in parts and archive is not None:
                    return archive
                if "keys" in parts and secret is not None:
                    return secret
                return separate_domains(path)
            return probe

        cases = {
            "archive_on_the_original_disk": probe_with(archive=volume([INTERNAL])),
            "archive_and_secret_on_one_disk": probe_with(archive=volume([EXTERNAL])),
            "disk_image": probe_with(secret=volume([EXTERNAL], virtual=True)),
            "share_this_machine_serves": probe_with(archive=volume(
                [], fstype="smbfs", source="//owner@localhost/escrow")),
            "no_device_facts": probe_with(secret=volume([], fstype="exfat")),
        }
        for label, probe in cases.items():
            with self.subTest(label=label), self.assertRaises(escrow.EscrowError):
                self.custody(probe=probe)

    def test_an_owner_attestation_stands_in_where_the_machine_cannot_prove_a_domain(self):
        self.create()

        def unprovable(path):
            return volume([]) if "keys" in Path(path).parts else separate_domains(path)

        with self.assertRaises(escrow.EscrowError):
            self.custody(probe=unprovable)
        with self.assertRaises(escrow.EscrowError):
            self.custody(probe=unprovable, secret_attestation="owner password manager")
        record = self.custody(probe=unprovable, secret_attestation="owner password manager",
                              attested_by="Owner")
        secret_domain = record["domains"]["secret"]
        self.assertEqual((secret_domain["kind"], secret_domain["machine_verified"],
                          secret_domain["attested_by"]), ("owner_attested", False, "Owner"))
        self.assertNotIn("password manager", json.dumps(record))
        self.assertEqual(escrow.verify_custody(record, private_dir=self.private), record)

    def test_mount_lines_and_network_hosts_are_read_on_both_platforms(self):
        self.assertEqual(escrow.parse_mount_line(
            "/dev/disk3s5 on /System/Volumes/Data (apfs, local, journaled)"),
            {"source": "/dev/disk3s5", "mount": "/System/Volumes/Data", "fstype": "apfs"})
        self.assertEqual(escrow.parse_mount_line("nas:/export on /mnt/escrow type nfs4 (rw)"),
                         {"source": "nas:/export", "mount": "/mnt/escrow", "fstype": "nfs4"})
        for source, host in (("//owner@NAS.example.test/escrow", "nas.example.test"),
                             ("nas.local:/export", "nas.local"),
                             ("owner@backup.example.test:/srv", "backup.example.test"),
                             ("https://dav.example.test/remote.php", "dav.example.test"),
                             ("/dev/disk4s1", "")):
            with self.subTest(source=source):
                self.assertEqual(escrow.network_host(source), host)

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("diskutil"),
                         "reads macOS disk descriptions")
    def test_two_directories_on_this_machine_are_one_failure_domain(self):
        self.create()
        self.assertEqual(escrow.failure_domain(self.archive)["identifier"],
                         escrow.failure_domain(self.private)["identifier"])
        with self.assertRaises(escrow.EscrowError):
            escrow.custody_record(private_dir=self.private, archive=self.archive,
                                  secret=self.secret)


if __name__ == "__main__":
    unittest.main()
