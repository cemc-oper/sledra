package speccontract

import (
	"bytes"
	"embed"
	"os"
	"path/filepath"
	"reflect"
)

// ContractVersion is the fixed write contract, independent of software versions.
const ContractVersion = "1.0.0"

//go:embed contract/*.json
var contracts embed.FS

// resources validates an explicit override against the installed contract before
// compiling anything. JSON formatting may differ; schema/catalog semantics may
// not. This prevents the context checker and embedded event codec from diverging.
// An empty directory selects embedded resources without any filesystem lookup.
func resources(specDir string) (map[string][]byte, error) {
	entries, err := contracts.ReadDir("contract")
	if err != nil {
		return nil, fail("contract_unavailable", "embedded contract unavailable")
	}
	result := make(map[string][]byte, len(entries))
	for _, entry := range entries {
		name := entry.Name()
		canonical, err := contracts.ReadFile("contract/" + name)
		if err != nil {
			return nil, fail("contract_unavailable", "embedded contract unavailable")
		}
		data := canonical
		if specDir != "" {
			data, err = os.ReadFile(filepath.Join(specDir, name))
			if err != nil {
				return nil, fail("contract_unavailable", "override resource unavailable: "+name)
			}
			if !bytes.Equal(data, canonical) {
				value, err := Decode(data)
				if err != nil {
					return nil, fail("contract_incompatible", "invalid override JSON: "+name)
				}
				want, err := Decode(canonical)
				if err != nil || !reflect.DeepEqual(value, want) {
					return nil, fail("contract_incompatible", "override differs from installed contract: "+name)
				}
			}
		}
		result[name] = data
	}
	return result, nil
}
