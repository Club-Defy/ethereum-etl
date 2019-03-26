# MIT License
#
# Copyright (c) 2018 Evgeny Medvedev, evge.medvedev@gmail.com
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.


import itertools
import logging
import os
import time
from collections import defaultdict

from web3 import Web3

from ethereumetl.file_utils import smart_open
from ethereumetl.jobs.export_blocks_job import ExportBlocksJob
from ethereumetl.jobs.export_receipts_job import ExportReceiptsJob
from ethereumetl.jobs.exporters.console_item_exporter import ConsoleItemExporter
from ethereumetl.jobs.exporters.in_memory_item_exporter import InMemoryItemExporter
from ethereumetl.jobs.extract_token_transfers_job import ExtractTokenTransfersJob
from ethereumetl.logging_utils import logging_basic_config

logging_basic_config()


def write_last_synced_block(file, last_synced_block):
    with smart_open(file, 'w') as last_synced_block_file:
        return last_synced_block_file.write(str(last_synced_block) + '\n')


def init_last_synced_block_file(start_block, last_synced_block_file):
    if os.path.isfile(last_synced_block_file):
        raise ValueError(
            '{} should not exist if --start-block option is specified. '
            'Either remove the {} file or the --start-block option.'
                .format(last_synced_block_file, last_synced_block_file))
    write_last_synced_block(last_synced_block_file, start_block)


def read_last_synced_block(file):
    with smart_open(file, 'r') as last_synced_block_file:
        return int(last_synced_block_file.read())


def join(left, right, join_fields, left_fields, right_fields):
    left_join_field, right_join_field = join_fields

    def field_list_to_dict(field_list):
        result_dict = {}
        for field in field_list:
            if isinstance(field, tuple):
                result_dict[field[0]] = field[1]
            else:
                result_dict[field] = field
        return result_dict

    left_fields_as_dict = field_list_to_dict(left_fields)
    right_fields_as_dict = field_list_to_dict(right_fields)

    left_map = defaultdict(list)
    for item in left: left_map[item[left_join_field]].append(item)

    right_map = defaultdict(list)
    for item in right: right_map[item[right_join_field]].append(item)

    for key in left_map.keys():
        for left_item, right_item in itertools.product(left_map[key], right_map[key]):
            result_item = {}
            for src_field, dst_field in left_fields_as_dict.items():
                result_item[dst_field] = left_item.get(src_field)
            for src_field, dst_field in right_fields_as_dict.items():
                result_item[dst_field] = right_item.get(src_field)

            yield result_item


def enrich_transactions(blocks, transactions, receipts):
    transactions_and_receipts = join(
        transactions, receipts, ('hash', 'transaction_hash'),
        left_fields=[
            'type',
            'hash',
            'nonce',
            'transaction_index',
            'from_address',
            'to_address',
            'value',
            'gas',
            'gas_price',
            'input',
            'block_number'
        ],
        right_fields=[
            ('cumulative_gas_used', 'receipt_cumulative_gas_used'),
            ('gas_used', 'receipt_gas_used'),
            ('contract_address', 'receipt_contract_address'),
            ('root', 'receipt_root'),
            ('status', 'receipt_status')
        ])

    result = join(
        transactions_and_receipts, blocks, ('block_number', 'number'),
        [
            'type',
            'hash',
            'nonce',
            'transaction_index',
            'from_address',
            'to_address',
            'value',
            'gas',
            'gas_price',
            'input',
            'block_number',
            'receipt_cumulative_gas_used',
            'receipt_gas_used',
            'receipt_contract_address',
            'receipt_root',
            'receipt_status'
        ],
        [
            ('timestamp', 'block_timestamp'),
            ('hash', 'block_hash'),
        ])
    return list(result)


def enrich_logs(blocks, logs):
    result = join(
        logs, blocks, ('block_number', 'number'),
        [
            'type',
            'log_index',
            'transaction_hash',
            'transaction_index',
            'address',
            'data',
            'topics',
            'block_number'
        ],
        [
            ('timestamp', 'block_timestamp'),
            ('hash', 'block_hash'),
        ])
    return list(result)


def enrich_token_transfers(blocks, token_transfers):
    result = join(
        token_transfers, blocks, ('block_number', 'number'),
        [
            'type',
            'token_address',
            'from_address',
            'to_address',
            'value',
            'transaction_hash',
            'log_index',
            'block_number'
        ],
        [
            ('timestamp', 'block_timestamp'),
            ('hash', 'block_hash'),
        ])
    return list(result)


def stream(
        batch_web3_provider,
        last_synced_block_file='last_synced_block.txt',
        lag=0,
        item_exporter=ConsoleItemExporter(),
        start_block=None,
        end_block=None,
        period_seconds=10,
        batch_size=100,
        block_batch_size=10,
        max_workers=5):
    if start_block is not None or not os.path.isfile(last_synced_block_file):
        init_last_synced_block_file((start_block or 0) - 1, last_synced_block_file)

    last_synced_block = read_last_synced_block(last_synced_block_file)

    item_exporter.open()

    while True and (end_block is None or last_synced_block < end_block):
        blocks_to_sync = 0

        try:
            current_block = int(Web3(batch_web3_provider).eth.getBlock("latest").number)
            target_block = current_block - lag
            target_block = min(target_block, last_synced_block + block_batch_size)
            target_block = min(target_block, end_block) if end_block is not None else target_block
            blocks_to_sync = max(target_block - last_synced_block, 0)
            logging.info('Current block {}, target block {}, last synced block {}, blocks to sync {}'.format(
                current_block, target_block, last_synced_block, blocks_to_sync))

            if blocks_to_sync == 0:
                logging.info('Nothing to sync. Sleeping {} seconds...'.format(period_seconds))
                time.sleep(period_seconds)
                continue

            # Export blocks and transactions
            blocks_and_transactions_item_exporter = InMemoryItemExporter(item_types=['block', 'transaction'])
            blocks_and_transactions_job = ExportBlocksJob(
                start_block=last_synced_block + 1,
                end_block=target_block,
                batch_size=batch_size,
                batch_web3_provider=batch_web3_provider,
                max_workers=max_workers,
                item_exporter=blocks_and_transactions_item_exporter,
                export_blocks=True,
                export_transactions=True
            )
            blocks_and_transactions_job.run()

            blocks = blocks_and_transactions_item_exporter.get_items('block')
            transactions = blocks_and_transactions_item_exporter.get_items('transaction')

            # Export receipts and logs
            receipts_and_logs_item_exporter = InMemoryItemExporter(item_types=['receipt', 'log'])
            receipts_and_logs_job = ExportReceiptsJob(
                transaction_hashes_iterable=(transaction['hash'] for transaction in transactions),
                batch_size=batch_size,
                batch_web3_provider=batch_web3_provider,
                max_workers=max_workers,
                item_exporter=receipts_and_logs_item_exporter,
                export_receipts=True,
                export_logs=True
            )
            receipts_and_logs_job.run()

            receipts = receipts_and_logs_item_exporter.get_items('receipt')
            logs = receipts_and_logs_item_exporter.get_items('log')

            # Extract token transfers
            token_transfers_item_exporter = InMemoryItemExporter(item_types=['token_transfer'])
            token_transfers_job = ExtractTokenTransfersJob(
                logs_iterable=logs,
                batch_size=batch_size,
                max_workers=max_workers,
                item_exporter=token_transfers_item_exporter)

            token_transfers_job.run()
            token_transfers = token_transfers_item_exporter.get_items('token_transfer')

            enriched_transactions = enrich_transactions(blocks, transactions, receipts)
            if len(enriched_transactions) != len(transactions):
                raise ValueError('The number of transactions is wrong ' + str(enriched_transactions))
            enriched_logs = enrich_logs(blocks, logs)
            if len(enriched_logs) != len(logs):
                raise ValueError('The number of logs is wrong ' + str(enriched_logs))
            enriched_token_transfers = enrich_token_transfers(blocks, token_transfers)
            if len(enriched_token_transfers) != len(token_transfers):
                raise ValueError('The number of token transfers is wrong ' + str(enriched_token_transfers))

            logging.info('Publishing to PubSub')
            item_exporter.export_items(blocks + enriched_transactions + enriched_logs + enriched_token_transfers)

            logging.info('Writing last synced block {}'.format(target_block))
            write_last_synced_block(last_synced_block_file, target_block)
            last_synced_block = target_block
        except Exception as e:
            # https://stackoverflow.com/a/4992124/1580227
            logging.exception('An exception occurred while fetching block data.')

        if blocks_to_sync != block_batch_size and last_synced_block != end_block:
            logging.info('Sleeping {} seconds...'.format(period_seconds))
            time.sleep(period_seconds)
