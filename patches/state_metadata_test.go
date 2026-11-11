package oras

import (
	"bytes"
	"context"
	"testing"

	"github.com/opentofu/opentofu/internal/encryption"
	"github.com/opentofu/opentofu/internal/states/remote"
	"github.com/opentofu/opentofu/internal/states/statefile"
	"github.com/opentofu/opentofu/internal/states/statemgr"
)

type metadataClient struct {
	data []byte
	puts int
}

func (c *metadataClient) Get(context.Context) (*remote.Payload, error) {
	if c.data == nil {
		return nil, nil
	}
	return &remote.Payload{Data: bytes.Clone(c.data)}, nil
}

func (c *metadataClient) Put(_ context.Context, data []byte) error {
	c.data = bytes.Clone(data)
	c.puts++
	return nil
}

func (c *metadataClient) Delete(context.Context) error {
	c.data = nil
	return nil
}

func metadataFixture(t *testing.T) *statefile.File {
	t.Helper()
	f, err := statefile.Read(bytes.NewBufferString(`{
		"version":4,"terraform_version":"1.13.9","serial":53,
		"lineage":"metadata-regression","outputs":{
			"fixture":{"value":"synthetic-only","type":"string"}
		},"resources":[]
	}`), encryption.StateEncryptionDisabled())
	if err != nil {
		t.Fatal(err)
	}
	return f
}

func TestHistoryStateMetadata(t *testing.T) {
	ctx := t.Context()
	client := &metadataClient{}
	mgr := &historyState{remoteState: remote.NewState(client, encryption.StateEncryptionDisabled())}
	if err := mgr.RefreshState(ctx); err != nil {
		t.Fatal(err)
	}
	if statemgr.Export(mgr) != nil {
		t.Fatal("absent remote exported a synthetic state")
	}
	source := metadataFixture(t)
	if err := statemgr.Import(source, mgr, false); err != nil {
		t.Fatal(err)
	}
	if err := mgr.PersistState(ctx, nil); err != nil {
		t.Fatal(err)
	}
	reader := &historyState{remoteState: remote.NewState(client, encryption.StateEncryptionDisabled())}
	if err := reader.RefreshState(ctx); err != nil {
		t.Fatal(err)
	}
	got := statemgr.Export(reader)
	if got.Lineage != source.Lineage || got.Serial != source.Serial ||
		!statefile.StatesMarshalEqual(got.State, source.State) {
		t.Fatal("first persistence lost imported metadata or content")
	}
	meta := reader.StateSnapshotMeta()
	if meta.Lineage != got.Lineage || meta.Serial != got.Serial {
		t.Fatal("saved-plan metadata does not match exported state")
	}
	if err := reader.PersistState(ctx, nil); err != nil || client.puts != 1 {
		t.Fatalf("unchanged state must not be written: %v, puts=%d", err, client.puts)
	}
	for _, conflict := range []string{"lineage", "older", "equal-different"} {
		t.Run(conflict, func(t *testing.T) {
			invalid := metadataFixture(t)
			switch conflict {
			case "lineage":
				invalid.Lineage = "unrelated"
			case "older":
				invalid.Serial--
			case "equal-different":
				invalid.State.RootModule().OutputValues = nil
			}
			if err := statemgr.Import(invalid, reader, false); err == nil {
				t.Fatal("unsafe import accepted")
			}
			if client.puts != 1 {
				t.Fatal("rejected import wrote state")
			}
		})
	}
	updated := metadataFixture(t)
	updated.Serial++
	if err := statemgr.Import(updated, reader, false); err != nil {
		t.Fatal(err)
	}
	if err := reader.PersistState(ctx, nil); err != nil {
		t.Fatal(err)
	}
	if got := statemgr.Export(reader); got.Lineage != source.Lineage || got.Serial <= source.Serial {
		t.Fatal("update did not preserve lineage and advance serial")
	}
}

func TestHistoryStateDestinationAppeared(t *testing.T) {
	client := &metadataClient{}
	mgr := &historyState{remoteState: remote.NewState(client, encryption.StateEncryptionDisabled())}
	if err := mgr.RefreshState(t.Context()); err != nil {
		t.Fatal(err)
	}
	if err := statemgr.Import(metadataFixture(t), mgr, false); err != nil {
		t.Fatal(err)
	}
	var existing bytes.Buffer
	if err := statefile.Write(metadataFixture(t), &existing, encryption.StateEncryptionDisabled()); err != nil {
		t.Fatal(err)
	}
	client.data = existing.Bytes()
	if err := mgr.PersistState(t.Context(), nil); err == nil {
		t.Fatal("concurrently created destination was silently overwritten")
	}
	if err := mgr.PersistState(t.Context(), nil); err == nil {
		t.Fatal("retry silently accepted a conflicting destination")
	}
	if client.puts != 0 {
		t.Fatal("conflicting destination was written")
	}
}
