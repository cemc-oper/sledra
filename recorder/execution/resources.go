package execution

import (
	"bytes"
	_ "embed"
	"io"
	"sync"

	"github.com/santhosh-tekuri/jsonschema/v5"
)

//go:embed contract/contract.schema.json
var contract []byte

var (
	schemasOnce sync.Once
	schemas     map[string]*jsonschema.Schema
	schemasErr  error
)

func loadSchemas() (map[string]*jsonschema.Schema, error) {
	schemasOnce.Do(func() {
		compiler := jsonschema.NewCompiler()
		// Every reference is embedded; an accidental external reference must fail offline.
		compiler.LoadURL = func(string) (io.ReadCloser, error) { return nil, bad("contract_unavailable") }
		if err := compiler.AddResource("execution.json", bytes.NewReader(contract)); err != nil {
			schemasErr = bad("contract_unavailable")
			return
		}
		schemas = make(map[string]*jsonschema.Schema)
		for _, kind := range []string{"root", "run", "event"} {
			schema, err := compiler.Compile("execution.json#/$defs/" + kind)
			if err != nil {
				schemasErr = bad("contract_unavailable")
				return
			}
			schemas[kind] = schema
		}
	})
	return schemas, schemasErr
}
