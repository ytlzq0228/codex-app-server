"""Downstream API-equivalent pricing from total and cached token subtotals."""
from decimal import Decimal


def priced_amount(input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, price):
    read, write = int(input_tokens), int(output_tokens)
    cached_read, cached_write = int(cache_read_tokens), int(cache_write_tokens)
    if min(read, write, cached_read, cached_write) < 0 or cached_read + cached_write > read:
        raise ValueError("Invalid token usage breakdown")
    amount = ((Decimal(read - cached_read - cached_write) * price.input_price)
              + (Decimal(write) * price.output_price)
              + (Decimal(cached_read) * price.cache_read_price)
              + (Decimal(cached_write) * price.cache_write_price))
    return amount / Decimal(1_000_000)
