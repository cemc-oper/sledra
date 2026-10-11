package speccontract

import (
	"encoding/json"
	"os"
	"testing"
)

func TestGoldenVectors(t *testing.T) {
	data, err := os.ReadFile("../../../spec/fixtures/1.0.0/golden-vectors.json")
	if err != nil {
		t.Fatal(err)
	}
	v, err := Decode(data)
	if err != nil {
		t.Fatal(err)
	}
	vectors := v.(map[string]any)
	data, err = os.ReadFile("../../../spec/fixtures/1.0.0/valid-bundle.json")
	if err != nil {
		t.Fatal(err)
	}
	v, err = Decode(data)
	if err != nil {
		t.Fatal(err)
	}
	bundle := v.(map[string]any)
	attempt := bundle["attempt_context"].(map[string]any)
	params := attempt["parameters"].(map[string]any)
	canon, err := Canonical(params)
	if err != nil {
		t.Fatal(err)
	}
	if string(canon) != vectors["canonical_parameters"] {
		t.Fatalf("parameters: %s", canon)
	}
	instance, err := TaskInstanceID(bundle["run_context"].(map[string]any)["run_id"].(string), "main.forecast", params)
	if err != nil {
		t.Fatal(err)
	}
	if instance != vectors["task_instance_id"] {
		t.Fatalf("instance: %s", instance)
	}
	if RegistryID("registry-synthetic-1", "run", "wsl-sandbox", "launch-coldstart-20261010") != vectors["run_id"] {
		t.Fatal("run ID")
	}
	if RegistryID("registry-synthetic-1", "attempt", instance, "exec-member01-first") != vectors["attempt_id"] {
		t.Fatal("attempt ID")
	}
	job := bundle["job_ref"].(map[string]any)
	jobID := "job_" + LengthDigest(job["platform_id"].(string), job["scheduler"].(string), job["cluster_id"].(string), job["scheduler_job_id"].(string), job["submission_instance"].(string))
	otherJobID := "job_" + LengthDigest(job["platform_id"].(string), job["scheduler"].(string), job["cluster_id"].(string), job["scheduler_job_id"].(string), "submit-b")
	if jobID != vectors["job_ref_id"] || otherJobID != vectors["reused_scheduler_job_id_ref"] || jobID == otherJobID {
		t.Fatal("job generation ID")
	}
	for _, test := range []struct {
		doc      map[string]any
		excluded []string
		key      string
	}{
		{bundle["definition_manifest"].(map[string]any), []string{"content_sha256", "definition_id"}, "manifest_sha256"},
		{bundle["run_context"].(map[string]any), []string{"context_sha256"}, "run_context_sha256"},
		{attempt, []string{"context_sha256"}, "attempt_context_sha256"},
	} {
		value, err := ContentSHA(test.doc, test.excluded...)
		if err != nil {
			t.Fatal(err)
		}
		if value != vectors[test.key] {
			t.Errorf("%s: %s", test.key, value)
		}
	}
	value, err := SHA(bundle["events"].([]any)[5])
	if err != nil {
		t.Fatal(err)
	}
	if value != vectors["event_sha256"] {
		t.Fatal("event hash")
	}
	for _, raw := range vectors["lead_inputs"].([]any) {
		input := raw.(map[string]any)
		num, _ := input["value"].(json.Number).Int64()
		if input["unit"] == "h" {
			num *= 3600
		}
		if num != 0 || input["normalized_lead_seconds"] != json.Number("0") {
			t.Fatal("lead normalization")
		}
	}
	negativeZero, err := Decode([]byte(`{"n":-0}`))
	if err != nil {
		t.Fatal(err)
	}
	zeroCanon, err := Canonical(negativeZero)
	if err != nil {
		t.Fatal(err)
	}
	if string(zeroCanon) != vectors["negative_zero_canonical"] {
		t.Fatal("negative zero canonicalization")
	}
	for _, test := range []struct {
		input any
		key   string
	}{
		{map[string]any{"label": "e\u0301", "emoji": "☀"}, "unicode_nfc"},
		{map[string]any{"z": json.Number("1"), "a": json.Number("2")}, "key_order"},
		{map[string]any{"member": "01"}, "string_01"},
		{map[string]any{"member": json.Number("1")}, "integer_1"},
	} {
		value, err := Canonical(test.input)
		if err != nil {
			t.Fatal(err)
		}
		if string(value) != vectors[test.key] {
			t.Errorf("%s: %s", test.key, value)
		}
	}
}
