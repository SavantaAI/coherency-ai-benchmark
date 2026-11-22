"""Shared argument rules rendered into the public and measured clients."""

NORMALIZE = '''
def normalize_id(record_id):
    if not isinstance(record_id, str):
        raise TypeError("record id must be a string")
    return record_id


def normalize_ids(ids):
    if len(ids) > 50:
        raise ValueError("get_many takes at most 50 ids")
    values = list(ids)
    if len(values) > 50:
        raise ValueError("get_many takes at most 50 ids")
    return [normalize_id(value) for value in values]
'''
