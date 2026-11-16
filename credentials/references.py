def snapshot_credential_ids(snapshot):
    return {
        value
        for field, value in snapshot.get("effective", {}).items()
        if field.endswith("credential_id") and type(value) is int
    }


def configuration_credential_ids(document):
    references = {
        row["id"]
        for row in document.get("credentials", [])
        if type(row.get("id")) is int
    }
    references.update(
        snapshot_credential_ids({"effective": document.get("global", {})})
    )
    for project in document.get("projects", []):
        references.update(snapshot_credential_ids(project))
    return references
