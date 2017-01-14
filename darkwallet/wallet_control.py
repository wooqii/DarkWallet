import asyncio
import sys
import traceback

from libbitcoin import bc

class WalletControlProcess:

    def __init__(self, client, model):
        self._procs = [
            QueryBlockchainReorganizationProcess(self, client, model),
            ScanStealthProcess(self, client, model),
            ScanHistoryProcess(self, client, model),
            MarkSentPaymentsConfirmedProcess(self, client, model),
            FillCacheProcess(self, client, model),
            GenerateKeysProcess(self, client, model)
        ]

    def wakeup_processes(self):
        [process.wakeup() for process in self._procs]

class BaseProcess:

    def __init__(self, parent, client, model):
        self.parent = parent
        self.client = client
        self.model = model

        self._wakeup_future = asyncio.Future()
        self._start()

    def _start(self):
        loop = asyncio.get_event_loop()
        self._task = loop.create_task(self._run())

    def wakeup(self):
        self._wakeup_future.set_result(None)

    async def _run(self):
        while True:
            try:
                await self.update()
            except:
                traceback.print_exc()
                raise
            try:
                await asyncio.wait_for(self._wakeup_future, 5)
            except asyncio.TimeoutError:
                pass
            finally:
                self._wakeup_future = asyncio.Future()

    async def update(self):
        pass

class QueryBlockchainReorganizationProcess(BaseProcess):

    def __init__(self, parent, client, model):
        super().__init__(parent, client, model)

        self._max_rewind_depth = 50

    async def update(self):
        head = await self._query_blockchain_head()
        if head is None:
            return

        last_height, header = head
        index = last_height, header.hash()

        if self.model.compare_indexes(index):
            # Nothing changed.
            return
        print("Last height:", last_height)
        print("Current height:", self.model.current_height)

        if self.model.current_index is None:
            print("Initializing new chain state.")
        elif header.previous_block_hash == self.model.current_hash:
            print("New single block added.")
        elif await self._index_is_connected(index):
            print("Several new blocks added.")
        else:
            print("Blockchain reorganization event.")
            self._invalidate_records()

        self._record(index)

        # Wakeup the other processes.
        self.parent.wakeup_processes()

    async def _query_blockchain_head(self):
        ec, height = await self.client.last_height()
        if ec:
            print("Error: querying last_height:", ec, file=sys.stderr)
            return None
        ec, header = await self.client.block_header(height)
        if ec:
            print("Error: querying header:", ec, file=sys.stderr)
            return None
        header = bc.Header.from_data(header)
        return height, header

    async def _index_is_connected(self, index, current_recursions=1):
        # To avoid long rewinds, if we recurse too much
        # just treat it as a reorganization event.
        if current_recursions > self._max_rewind_depth:
            print("Exceeded max rewind depth.")
            return False

        height, hash_ = index
        print("Rewinding from:", index)

        if height <= self.model.current_height:
            print("Rewinded past current index.")
            return False

        ec, header = await self.client.block_header(height)
        if ec:
            print("Error: querying header:", ec, file=sys.stderr)
            return False
        header = bc.Header.from_data(header)

        if header.hash() != hash_:
            print("Error: non-matching header and index hash.",
                  file=sys.stderr)
            return False

        # Try to link this block with the current recorded hash.
        if header.previous_block_hash == self.model.current_hash:
            return True

        # Run the check for the next block along now.
        previous_index = height - 1, header.previous_block_hash

        return await self._index_is_connected(previous_index,
                                              current_recursions + 1)

    # ------------------------------------------------
    # Invalidate records because of reorganization.
    # ------------------------------------------------

    def _invalidate_records(self):
        print("Invalidating records...")
        self._clear_history()
        print("Cleared history.")
        self._nullify_address_updated_heights()
        print("Reset address updated heights.")

    def _clear_history(self):
        self.model.cache.history.clear()

    def _nullify_address_updated_heights(self):
        self.model.cache.track_address_updates.clear()

    # ------------------------------------------------
    # Finish by writing the new current index.
    # ------------------------------------------------

    def _record(self, index):
        print("Updating current_index to:", index)
        self.model.current_index = index

class ScanStealthProcess(BaseProcess):

    @property
    def _stealth_addrs(self):
        return [pocket.stealth_address for pocket in self.model.pockets]

    async def update(self):
        heights = []
        for stealth_address in self.model.stealth_addrs:
            tracker = self.model.cache.track_address_updates
            last_updated_height = tracker.last_updated_height(stealth_address)
            heights.append(last_updated_height)

        from_height = min(heights)
        await self._query_stealth(from_height)

    async def _query_stealth(self, from_height):
        genesis_height = 0
        if self.model.is_testnet:
            genesis_height = 1063370
        from_height = max(genesis_height, from_height)
        # We haven't implemented prefixes yet.
        prefix = libbitcoin.server.Binary(0, b"")
        print("Starting stealth query. [from_height=%s]" % from_height)
        ec, rows = await self.client.stealth(prefix, from_height)
        print("Stealth query done.")
        if ec:
            print("Error: query stealth:", ec, file=sys.stderr)
            return
        for ephemkey, address_hash, tx_hash in rows:
            ephemeral_public = bytes([2]) + ephemkey[::-1]
            ephemeral_public = bc.EcCompressed.from_bytes(ephemeral_public)

            version = self.model.payment_address_version()

            address = bc.PaymentAddress.from_hash(address_hash[::-1],
                                                  version)

            tx_hash = bc.HashDigest.from_bytes(tx_hash[::-1])

            await self._scan_all_pockets_for_stealth(ephemeral_public,
                                                     address, tx_hash)

    async def _scan_all_pockets_for_stealth(self, ephemeral_public,
                                            original_address, tx_hash):
        for pocket in self.model.pockets:
            await self._scan_pocket_for_stealth(pocket, ephemeral_public,
                                                original_address, tx_hash)

    async def _scan_pocket_for_stealth(self, pocket, ephemeral_public,
                                       original_address, tx_hash):
        receiver = pocket.stealth_receiver
        derived_address = receiver.derive_address(ephemeral_public)
        if derived_address is None or original_address != derived_address:
            return
        assert original_address == derived_address
        print("Found match:", derived_address)

        private_key = receiver.derive_private(ephemeral_public)
        pocket.add_stealth_key(original_address, private_key)

class ScanHistoryProcess(BaseProcess):

    async def update(self):
        tasks = []
        for pocket in self.model.pockets:
            tasks += [
                self._process(address, pocket) for address in pocket.addrs
            ]

        # Remove all the None values
        tasks = [task for task in tasks if task is not None]

        await asyncio.gather(*tasks)

    def _process(self, address, pocket):
        tracker = self.model.cache.track_address_updates

        from_height = tracker.last_updated_height(address)

        if from_height == self.model.current_height:
            return None
        assert from_height < self.model.current_height

        coroutine = self._scan(address, from_height, pocket)
        return coroutine

    async def _scan(self, address, from_height, pocket):
        ec, history = await self.client.history(address.encoded())
        if ec:
            print("Couldn't fetch history:", ec, file=sys.stderr)
            return

        print("Fetched history for", address)

        self._set_history(address, history, pocket)

        self._mark_address_updated(address)

    def _set_history(self, address, history, pocket):
        self.model.cache.history.set(address, history, pocket)

    def _mark_address_updated(self, address):
        last_height = self.model.current_height
        self.model.set_last_updated_height(address, last_height)

class MarkSentPaymentsConfirmedProcess(BaseProcess):

    async def update(self):
        self.mark_any_confirmed_sent_payments()

class FillCacheProcess(BaseProcess):

    async def update(self):
        await self._fill_cache()

    async def _fill_cache(self):
        for tx_hash in self.model.cache.history.transaction_hashes:
            if not tx_hash in self.model.cache.transactions:
                await self._grab_tx(tx_hash)

    async def _grab_tx(self, tx_hash):
        ec, tx_data = await self.client.transaction(tx_hash.data)
        if ec:
            print("Couldn't fetch transaction:", ec, file=sys.stderr)
            return
        print("Got tx:", tx_hash)
        tx = bc.Transaction.from_data(tx_data)
        self.model.cache.transactions[tx_hash] = tx

class GenerateKeysProcess(BaseProcess):

    async def update(self):
        await self._generate_keys()

    async def _generate_keys(self):
        for pocket in self.model.pockets:
            self._generate_pocket_keys(pocket)

    def _generate_pocket_keys(self, pocket):
        max_i = -1
        for address in pocket.addrs:
            if address not in self.model.cache.history:
                continue
            i = pocket.address_index(address)
            if i is None:
                continue
            max_i = max(i, max_i)
        desired_len = max_i + 1 + self._settings.gap_limit
        remaining = desired_len - pocket.number_normal_keys()
        assert remaining >= 0
        for i in range(remaining):
            pocket.add_key()
        print("Generated %s keys" % remaining)

