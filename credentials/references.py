def snapshot_credential_ids(snapshot):
    return {
        value
        for field, value in snapshot.get("effective", {}).items()
        if field.endswith("credential_id") and type(value) is int
    }
