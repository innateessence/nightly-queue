import json

from django import template

register = template.Library()


@register.filter
def pretty_json(value):
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False)
