import copy
import unittest

from bound_revision import bounded, revision_request


class RevisionTest(unittest.TestCase):
    def setUp(self):
        self.image = "cr.yandex/example/probe@sha256:" + "a" * 64
        self.inputs = {"probe_image": self.image, "runtime_service_account_id": "runtime"}
        self.revision = {
            "id": "revision", "containerId": "container", "status": "ACTIVE",
            "serviceAccountId": "runtime", "resources": {"memory": "268435456"},
            "image": {"imageUrl": self.image, "imageDigest": "sha256:" + "a" * 64,
                      "environment": {"MODE": "probe"}},
            "secrets": [{"id": "secret", "versionId": "version", "key": "xray_config"}],
            "mounts": [{"mountPointPath": "/publication", "mode": "READ_ONLY"}],
            "metadataOptions": {"gceHttpEndpoint": "ENABLED"},
        }

    def test_preserves_runtime_configuration(self):
        original = copy.deepcopy(self.revision)
        payload = revision_request(self.revision, "container", self.inputs)
        self.assertEqual(payload["secrets"], original["secrets"])
        self.assertEqual(payload["mounts"], original["mounts"])
        self.assertEqual(payload["metadataOptions"], original["metadataOptions"])
        self.assertEqual(payload["imageSpec"]["environment"], {"MODE": "probe"})
        self.assertNotIn("imageDigest", payload["imageSpec"])
        self.assertEqual(original, self.revision)
        self.assertTrue(bounded(payload))

    def test_rejects_wrong_revision_or_unknown_fields(self):
        for key, value in (("containerId", "other"), ("serviceAccountId", "other"),
                           ("status", "OBSOLETE"), ("newRuntimeSetting", {})):
            with self.subTest(key=key), self.assertRaises(ValueError):
                revision_request({**self.revision, key: value}, "container", self.inputs)

    def test_rejects_other_image(self):
        self.revision["image"]["imageUrl"] = "cr.yandex/example/probe:latest"
        with self.assertRaises(ValueError):
            revision_request(self.revision, "container", self.inputs)

    def test_application_uses_its_own_selected_digest(self):
        self.inputs["application_image"] = self.image
        self.inputs["probe_image"] = "cr.yandex/example/probe@sha256:" + "b" * 64
        payload = revision_request(self.revision, "container", self.inputs, "application_image")
        self.assertEqual(payload["imageSpec"]["imageUrl"], self.image)
        with self.assertRaises(ValueError):
            revision_request(self.revision, "container", self.inputs)

    def test_limits_must_both_match(self):
        self.assertFalse(bounded(self.revision))
        self.assertFalse(bounded({"scalingPolicy": {"zoneInstancesLimit": 1}}))
        self.assertTrue(bounded({"scalingPolicy": {"zoneInstancesLimit": 1, "zoneRequestsLimit": 1}}))

    def test_only_exact_legacy_mount_duplicate_is_removed(self):
        self.revision["storageMounts"] = [{"mountPointPath": "/publication", "bucketId": "bucket",
                                            "prefix": "m1/", "readOnly": True}]
        self.revision["mounts"][0]["objectStorage"] = {"bucketId": "bucket", "prefix": "m1/"}
        payload = revision_request(self.revision, "container", self.inputs)
        self.assertEqual(payload["storageMounts"], [])
        self.assertEqual(payload["mounts"], self.revision["mounts"])
        self.revision["storageMounts"][0]["readOnly"] = False
        with self.assertRaises(ValueError):
            revision_request(self.revision, "container", self.inputs)


if __name__ == "__main__":
    unittest.main()
