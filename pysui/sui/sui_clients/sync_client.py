#    Copyright Frank V. Castellucci
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#        http://www.apache.org/licenses/LICENSE-2.0
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

# -*- coding: utf-8 -*-
# pylint: disable=too-many-instance-attributes, too-many-public-methods, line-too-long

"""Sui Synchronous RPC Client module."""

import logging
from typing import Any, Optional, Union
from json import JSONDecodeError
import ssl
import httpx
from deprecated.sphinx import versionchanged, versionadded, deprecated
from pysui import (
    PreExecutionResult,
    SuiRpcResult,
    handle_result,
    ObjectID,
    SuiAddress,
    SuiConfig,
)
from pysui.sui.sui_clients.common import _ClientMixin

from pysui.sui.sui_crypto import MultiSig, SuiPublicKey
from pysui.sui.sui_types.scalars import (
    SuiInteger,
    SuiSignature,
    SuiTxBytes,
    SuiString,
)
from pysui.sui.sui_types.collections import SuiArray, SuiMap
from pysui.sui.sui_txresults.single_tx import (
    FaucetGasRequest,
    ObjectReadPage,
    SuiCoinObjects,
)
from pysui.sui.sui_builders.base_builder import SuiBaseBuilder, SuiRequestType
from pysui.sui.sui_builders.get_builders import (
    GetCoinTypeBalance,
    GetCoins,
    GetMultipleObjects,
    GetPastObject,
    GetObjectsOwnedByAddress,
    GetObject,
    GetPackage,
    QueryEvents,
)
from pysui.sui.sui_builders.exec_builders import (
    DryRunTransaction,
    ExecuteTransaction,
    Pay,
    PaySui,
    PayAllSui,
    MergeCoin,
    SplitCoin,
    SplitCoinEqually,
    TransferObject,
    TransferSui,
    MoveCall,
    Publish,
)
from pysui.sui.sui_utils import partition

# Standard library logging setup
logger = logging.getLogger("pysui.sync_client")
if not logging.getLogger().handlers:
    logger.addHandler(logging.NullHandler())
    logger.propagate = False


class SuiClient(_ClientMixin):
    """Sui Syncrhonous Client."""

    @versionchanged(version="0.28.0", reason="Added logging")
    def __init__(
        self,
        config: SuiConfig,
        request_type: SuiRequestType = SuiRequestType.WAITFORLOCALEXECUTION,
    ) -> None:
        """Client initializer."""
        super().__init__(config, request_type)
        self._client = httpx.Client(
            http2=True,
            timeout=120.0,
            verify=ssl.SSLContext(ssl.PROTOCOL_SSLv23),
        )
        self._fetch_common_descriptors()
        logger.info(f"Initialized synchronous client for {config.rpc_url}")

    @property
    def is_synchronous(self) -> bool:
        """Return whether client is syncrhonous (True) or not (False)."""
        return True

    @versionchanged(
        version="0.28.0", reason="Consolidated exception handling."
    )
    def _execute(
        self, builder: SuiBaseBuilder
    ) -> Union[SuiRpcResult, Exception]:
        """Execute the builder construct."""
        # Validate builder and send request
        try:
            result = self._client.post(
                self.config.rpc_url,
                headers=builder.header,
                json=self._validate_builder(builder),
            )
            return SuiRpcResult(
                True,
                None,
                result.json(),
            )
        except JSONDecodeError as jexc:
            return SuiRpcResult(
                False, f"JSON Decoder Error {jexc.msg}", vars(jexc)
            )
        except (
            httpx.HTTPError,
            httpx.InvalidURL,
            httpx.CookieConflict,
        ) as hexc:
            return SuiRpcResult(
                False, f"HTTPX error: {hexc.__class__.__name__}", vars(hexc)
            )

    def execute(
        self,
        builder: SuiBaseBuilder,
        additional_signatures: SuiArray[SuiAddress] = None,
    ) -> Union[SuiRpcResult, Exception]:
        """Execute the builder construct."""
        if not builder.txn_required:
            result = self._execute(builder)
            if result.is_ok():
                if "error" in result.result_data:
                    return SuiRpcResult(
                        False, result.result_data["error"], None
                    )
                # print(result.result_data)
                return SuiRpcResult(
                    True,
                    None,
                    builder.handle_return(result.result_data["result"]),
                )
            return result
        return self._multi_signed_execution(builder, additional_signatures)

    def execute_no_sign(
        self, builder: SuiBaseBuilder
    ) -> Union[SuiRpcResult, Exception]:
        """Submit transaction and returns the signer and transaction bytes in the result_data as tuple."""
        if builder.txn_required:
            result = self._execute(builder)
            if result.is_ok():
                result = result.result_data
                if "error" in result:
                    return SuiRpcResult(
                        False, result["error"]["message"], None
                    )
                result = SuiRpcResult(
                    True,
                    None,
                    PreExecutionResult(
                        builder.authority,
                        builder.handle_return(result["result"]),
                    ),
                )
            return result
        return SuiRpcResult(
            False, "execute_no_sign is used only with transaction types"
        )

    @versionchanged(
        version="0.16.1", reason="Support sponsored transaction signing."
    )
    def execute_with_multisig(
        self,
        builder: SuiBaseBuilder,
        multi_sig: MultiSig,
        pub_keys: list[SuiPublicKey],
        signers: Optional[SuiArray[SuiAddress]] = None,
    ) -> Union[SuiRpcResult, Exception]:
        """execute_with_multisig Executes a transaction, signing with MultiSig."""
        if builder.txn_required:
            result = self.execute_no_sign(builder)
            if result.is_ok():
                tx_bytes = result.result_data.tx_bytes
                new_sig = multi_sig.sign(tx_bytes, pub_keys)
                if isinstance(new_sig, SuiSignature):
                    sig_array = [new_sig]
                    if signers:
                        sig_array.extend(
                            [
                                kpair.new_sign_secure(tx_bytes)
                                for kpair in signers.array
                            ]
                        )
                    exec_tx = ExecuteTransaction(
                        tx_bytes=tx_bytes,
                        signatures=SuiArray(sig_array),
                        request_type=SuiRequestType.WAITFORLOCALEXECUTION,
                    )
                    return self.execute(exec_tx)
                return SuiRpcResult(False, f"Signing error code {new_sig}")
            return result
        return self._execute(builder)

    def sign_and_submit(
        self,
        signer: SuiAddress,
        tx_bytes: SuiTxBytes,
        additional_signatures: SuiArray[SuiAddress] = None,
    ) -> Union[SuiRpcResult, Exception]:
        """sign_and_submit Signs the transaction bytes from previous submission, signs and executes.

        :param signer: Signer for transaction. Should be the same from original transaction
        :type signer: SuiAddress
        :param tx_bytes: Transaction bytes from previous submission
        :type tx_bytes: SuiTxBytes
        :param additional_signatures: Array of additional signatures to sign with, defaults to None
        :type additional_signatures: SuiArray[SuiAddress], optional
        :return: Result of execution
        :rtype: Union[SuiRpcResult, Exception]
        """
        signers_list = []
        signers_list.append(self.config.keypair_for_address(signer))
        if additional_signatures:
            for addy in additional_signatures.array:
                signers_list.append(self.config.keypair_for_address(addy))
        builder = ExecuteTransaction(
            tx_bytes=tx_bytes,
            signatures=SuiArray(
                [
                    kpair.new_sign_secure(tx_bytes.tx_bytes)
                    for kpair in signers_list
                ]
            ),
            request_type=self.request_type,
        )
        result = self._execute(builder)
        if result.is_ok() and "error" not in result.result_data:
            # print(result.result_data["result"])
            result = SuiRpcResult(
                True, None, builder.handle_return(result.result_data["result"])
            )
        return result

    def dry_run(
        self, builder: SuiBaseBuilder
    ) -> Union[SuiRpcResult, Exception]:
        """Submit transaction than sui_dryRunTransaction only."""
        if builder.txn_required:
            result = self.execute_no_sign(builder)
            if result.is_ok():
                result = self.execute(
                    DryRunTransaction(tx_bytes=result.result_data.tx_bytes)
                )
            return result
        return SuiRpcResult(
            False, "dry_run is used only with transaction types"
        )

    def _multi_signed_execution(
        self,
        builder: SuiBaseBuilder,
        additional_signers: SuiArray[SuiAddress] = None,
    ) -> Union[SuiRpcResult, Exception]:
        """."""
        result = self.execute_no_sign(builder)
        if result.is_ok():
            exec_builder = self.sign_for_execution(
                result.result_data.tx_bytes, builder, additional_signers
            )
            result = self._execute(exec_builder)
            if result.is_ok() and "error" not in result.result_data:
                # print(result.result_data["result"])
                result = SuiRpcResult(
                    True,
                    None,
                    exec_builder.handle_return(result.result_data["result"]),
                )
            else:
                result = SuiRpcResult(False, result.result_data["error"])
        return result

    # Build and execute convenience methods

    @versionchanged(
        version="0.28.0",
        reason="Support for > RPC provider limits of coin objects for owner.",
    )
    def _get_coins_for_type(
        self,
        *,
        address: SuiAddress,
        coin_type: SuiString = SuiString("0x2::sui::SUI"),
        fetch_all: Optional[bool] = False,
    ) -> SuiRpcResult:
        """_get_coins_for_type Returns all the coins of type for an address.

        :param address: The address to fetch coins of coin_type for
        :type address: SuiAddress
        :param coin_type: Fully qualified type names for the coin (e.g., 0x168da5bf1f48dafc111b0a488fa454aca95e0b5e::usdc::USDC),
        defaults to SuiString("0x2::sui::SUI")
        :type coin_type: SuiString, optional
        :return: If successful, result contains an array of coins objects of coin_type found
        :rtype: SuiRpcResult
        """
        result = self.execute(
            GetCoinTypeBalance(owner=address, coin_type=coin_type)
        )
        if result.is_ok():
            limit: int = result.result_data.coin_object_count
            builder = GetCoins(owner=address, coin_type=coin_type)
            if limit > self.max_gets and fetch_all:
                accumer: list = []
                gasses = handle_result(self.execute(builder))
                accumer.extend(gasses.data)
                while gasses.next_cursor:
                    builder.cursor = gasses.next_cursor
                    gasses = handle_result(self.execute(builder))
                    accumer.extend(gasses.data)
                result = SuiRpcResult(
                    True,
                    "",
                    SuiCoinObjects.from_dict(
                        {"data": accumer, "next_cursor": None}
                    ),
                )
            # if < 50 or > 50 and fetch_all false
            # legacy behavior
            else:
                result = self.execute(builder)
        return result

    @versionadded(
        version="0.26.1",
        reason="Implement closing underlying httpx transport.",
    )
    def close(self):
        """Closes the httpx transport connection.

        Does not usually need to be called but put in place as part of commingling with websocket activity.
        """
        self._client.close()
        self._transport_open = False

    @versionchanged(
        version="0.28.0",
        reason="Added fetch_all as currently limited by RPC providers results",
    )
    def get_gas(
        self, address: SuiAddress = None, fetch_all: Optional[bool] = False
    ) -> SuiRpcResult:
        """get_gas Retrieves SUI gas coin objects for address.

        :param address: If None, active_address will be used, defaults to None
        :type address: SuiAddress, optional
        :param fetch_all: Flag indicating to fetch all coins, defaults to False
            Max coins are set by RPC Provider (currently 50 for Sui MystenLab providers)
        :type fetch_all: Optional[bool], optional
        :return: If successful, result contains an array of SUI gas objects found
        :rtype: SuiRpcResult
        """
        address = address or self.config.active_address
        return self._get_coins_for_type(address=address, fetch_all=fetch_all)

    @versionchanged(
        version="0.28.0",
        reason="Added fetch_all as currently limited by RPC providers results",
    )
    def get_coin(
        self,
        coin_type: SuiString,
        address: SuiAddress = None,
        fetch_all: Optional[bool] = False,
    ) -> SuiRpcResult:
        """get_coin Retrieves objects of coin_type for address.

        :param coin_type: Fully qualified type names for the coin (e.g., 0x168da5bf1f48dafc111b0a488fa454aca95e0b5e::usdc::USDC)
        :type coin_type: SuiString
        :param address: If None, active_address will be used, defaults to None
        :type address: SuiAddress, optional
        :param fetch_all: Flag indicating to fetch all coins, defaults to False
            Max coins are set by RPC Provider (currently 50 for Sui MystenLab providers)
        :type fetch_all: Optional[bool], optional
        :return: If successful, result contains an array of coins objects of coin_type found
        :rtype: SuiRpcResult
        """
        address = address or self.config.active_address
        return self._get_coins_for_type(
            address=address, coin_type=coin_type, fetch_all=fetch_all
        )

    def get_gas_from_faucet(self, for_address: SuiAddress = None) -> Any:
        """get_gas_from_faucet Gets gas from SUI faucet.

        **Note**: This is only for devnet, testnet and local usage.
        :param for_address: Address to transfer faucet gas to, defaults to None
        :type for_address: SuiAddress, optional
        :return: _description_
        :rtype: Any
        """
        for_address = for_address or self.config.active_address
        try:
            result = self._client.post(
                self.config.faucet_url,
                headers=GetObjectsOwnedByAddress(for_address).header,
                json={"FixedAmountRequest": {"recipient": f"{for_address}"}},
            ).json()
            if result["error"] is None:
                return SuiRpcResult(
                    True, None, FaucetGasRequest.from_dict(result)
                )
            return SuiRpcResult(False, result["error"])
        except JSONDecodeError as jexc:
            return SuiRpcResult(
                False, f"JSON Decoder Error {jexc.msg}", vars(jexc)
            )
        except httpx.ReadTimeout as hexc:
            return SuiRpcResult(False, "HTTP read timeout error", vars(hexc))
        except TypeError as texc:
            return SuiRpcResult(False, "Type error", vars(texc))

    def get_object(
        self, identifier: ObjectID, version: SuiInteger = None
    ) -> Union[SuiRpcResult, Exception]:
        """get_object Get specific object by it's identifier and version if provided.

        :param identifier: The object identifier of object to fetch
        :type identifier: ObjectID
        :param version: The objects version sequence number, defaults to None
        :type version: SuiInteger, optional
        :return: The objeect's ObjectRead data
        :rtype: Union[SuiRpcResult, Exception]
        """
        return self.execute(
            GetObject(object_id=identifier)
            if version is None
            else GetPastObject(identifier, version)
        )

    @versionchanged(
        version="0.28.0",
        reason="Added fetch_all as currently limited by RPC providers results",
    )
    def get_objects(
        self,
        address: SuiAddress = None,
        fetch_all: Optional[bool] = False,
    ) -> Union[SuiRpcResult, Exception]:
        """get_objects Returns objects owned by address.

        :param address: Address to object ownership, defaults to None
            Will use active-address if None
        :type address: SuiAddress, optional
        :param fetch_all: Flag indicating to fetch all objects, defaults to False
            Max objects are set by RPC Provider (currently 50 for Sui MystenLab providers)
        :type fetch_all: Optional[bool], optional
        :return: list of owned objects
        :rtype: Union[SuiRpcResult,Exception]
        """
        builder = GetObjectsOwnedByAddress(
            address if address else self.config.active_address
        )
        result = self.execute(builder)
        if result.is_ok():
            if result.result_data.next_cursor and fetch_all:
                objects = result.result_data
                accumer: list = []
                accumer.extend(objects.data)
                while objects.next_cursor and objects.has_next_page:
                    builder.cursor = objects.next_cursor
                    objects = handle_result(self.execute(builder))
                    accumer.extend(objects.data)
                objread_page = ObjectReadPage.from_dict(
                    {
                        "data": [],
                        "has_next_page": False,
                        "next_cursor": None,
                    }
                )
                objread_page.data = accumer
                result = SuiRpcResult(True, "", objread_page)
        return result

    @versionchanged(
        version="0.29.0",
        reason="Handles large identifier list",
    )
    def get_objects_for(
        self, identifiers: list[ObjectID]
    ) -> Union[SuiRpcResult, Exception]:
        """get_objects_for Get objects for the list of identifiers.

        :param identifiers: The list of ObjectID's being queried.
        :type identifiers: list
        :raises: :class:`SuiException`: if returned from `self.execute`

        :returns: A list of object data
        :rtype: SuiRpcResult
        """
        # Revert to new multiOp
        builder = GetMultipleObjects(object_ids=[])
        # Handle large list
        if len(identifiers) > self.max_gets:
            accum: list = []
            for chunk in list(partition(identifiers, self.max_gets)):
                builder.object_ids = chunk
                accum.extend(handle_result(self.execute(builder)))
            result = SuiRpcResult(True, None, accum)
        else:
            builder.object_ids = identifiers
            result = self.execute(builder)

        return result

    def get_package(
        self, package_id: ObjectID
    ) -> Union[SuiRpcResult, Exception]:
        """get_package Get details of Sui package.

        :param package_id: The ObjectID of object being queried.
        :type package_id: ObjectID
        :raises: :class:`SuiException`: if returned from `self.execute`

        :returns: The package detail data
        :rtype: SuiRpcResult
        """
        result = self.execute(GetPackage(package=package_id))
        return result

    def get_events(
        self,
        *,
        query: SuiMap,
        cursor: str = "",
        limit: int = 50,
        descending_order: bool = True,
    ) -> SuiRpcResult:
        """get_events `sui_getEvents` API.

        :param query: The event query type map.
        :type query: SuiMap
        :param cursor: _description_
        :type cursor: str
        :param limit: _description_
        :type limit: int
        :param descending_order: _description_
        :type descending_order: bool
        :return: API call result
        :rtype: SuiRpcResult
        """
        inargs: dict = locals().copy()
        inargs.pop("self")
        return self.execute(QueryEvents(**inargs))

    @deprecated(version="0.28.0", reason="Use SuiTransaction builder instead.")
    def pay_txn(
        self,
        *,
        signer: SuiAddress,
        input_coins: SuiArray[ObjectID],
        recipients: SuiArray[SuiAddress],
        amounts: SuiArray[SuiInteger],
        gas: ObjectID,
        gas_budget: SuiInteger,
    ) -> SuiRpcResult:
        """pay_txn From the input Coin<T> coins, pay receipients the amounts for each.

        :param signer: **Required**: Signer address for transaction and owner of the input_coins
        :type signer: SuiAddress
        :param input_coins: **Required**: Array of 1 or more input coin Coin<T> ObjectIDs,
            including the coin for gas payment
        :type input_coins: SuiArray[ObjectID]
        :param recipients: **Required**: Array of recipient addresses of coins being paid to.
            The length of this array must be the same as amounts
        :type recipients: SuiArray[SuiAddress]
        :param amounts: **Required**: The amounts to be transferred to recipients, following the same order
        :type amounts: SuiArray[SuiInteger]
        :param gas: **Required**: Gas object ObjectID to be used to pay for this transaction
        :type gas: ObjectID
        :param gas_budget: **Required**: Amount of gas to pay for transaction (taken from first of input coins)
        :type gas_budget: SuiInteger
        :return: Result of the transaction
        :rtype: SuiRpcResult
        """
        logger.warning("using deprecated method: pay_txn")
        inargs: dict = locals().copy()
        inargs.pop("self")
        return self.execute(Pay(**inargs))

    @deprecated(version="0.28.0", reason="Use SuiTransaction builder instead.")
    def pay_sui_txn(
        self,
        *,
        signer: SuiAddress,
        input_coins: SuiArray[ObjectID],
        recipients: SuiArray[SuiAddress],
        amounts: SuiArray[SuiInteger],
        gas_budget: SuiInteger,
    ) -> SuiRpcResult:
        """pay_sui_txn From the input Sui Gas coins, pay receipients the amounts for each.

        :param signer: **Required**: Signer address for transaction and owner of the input_coins
        :type signer: SuiAddress
        :param input_coins: **Required**: Array of 1 or more input coin Sui Gas ObjectIDs,
            including the coin for gas payment
        :type input_coins: SuiArray[ObjectID]
        :param recipients: **Required**: Array of recipient addresses of coins being paid to.
            The length of this array must be the same as amounts
        :type recipients: SuiArray[SuiAddress]
        :param amounts: **Required**: The amounts to be transferred to recipients, following the same order
        :type amounts: SuiArray[SuiInteger]
        :param gas_budget: **Required**: Amount of gas to pay for transaction (taken from first of input coins)
        :type gas_budget: SuiInteger
        :return: Result of the transaction
        :rtype: SuiRpcResult
        """
        logger.warning("using deprecated method: pay_sui_txn")
        inargs: dict = locals().copy()
        inargs.pop("self")
        return self.execute(PaySui(**inargs))

    @deprecated(version="0.28.0", reason="Use SuiTransaction builder instead.")
    def pay_allsui_txn(
        self,
        *,
        signer: SuiAddress,
        input_coins: SuiArray[ObjectID],
        recipient: SuiAddress,
        gas_budget: SuiInteger,
    ) -> SuiRpcResult:
        """pay_allsui_txn Send all input coins to recipient.

        :param signer: **Required**: Signer address for transaction and owner of the input_coins
        :type signer: SuiAddress
        :param input_coins: **Required**: Array of 1 or more input Sui Gas ObjectIDs
        :type input_coins: SuiArray
        :param recipient: **Required**: Recipient address of coins being paid to
        :type recipient: SuiAddress
        :param gas_budget: **Required**: Amount of gas to pay for transaction (taken from first of input coins)
        :type gas_budget: SuiInteger
        :return: Result of the transaction
        :rtype: SuiRpcResult
        """
        logger.warning("using deprecated method: pay_allsui_txn")
        inargs: dict = locals().copy()
        inargs.pop("self")
        return self.execute(PayAllSui(**inargs))

    @deprecated(version="0.28.0", reason="Use SuiTransaction builder instead.")
    def transfer_sui_txn(
        self,
        *,
        signer: SuiAddress,
        sui_object_id: ObjectID,
        gas_budget: SuiInteger,
        recipient: SuiAddress,
        amount: SuiInteger,
    ) -> SuiRpcResult:
        """transfer_sui_txn invokes `sui_transferSui` API.

        :param signer: _description_
        :type signer: SuiAddress
        :param sui_object_id: _description_
        :type sui_object_id: ObjectID
        :param gas_budget: _description_
        :type gas_budget: SuiInteger
        :param recipient: _description_
        :type recipient: SuiAddress
        :param amount: _description_
        :type amount: SuiInteger
        :return: Result of the transaction
        :rtype: SuiRpcResult
        """
        logger.warning("using deprecated method: transfer_sui_txn")
        inargs: dict = locals().copy()
        inargs.pop("self")
        return self.execute(TransferSui(**inargs))

    @deprecated(version="0.28.0", reason="Use SuiTransaction builder instead.")
    def transfer_object_txn(
        self,
        *,
        signer: SuiAddress,
        object_id: ObjectID,
        gas: ObjectID,
        gas_budget: SuiInteger,
        recipient: SuiAddress,
    ) -> SuiRpcResult:
        """transfer_object_txn invokes `sui_transferObject` API.

        :param signer: Transaction signer and owner of object_id
        :type signer: SuiAddress
        :param object_id: The object being transferred
        :type object_id: ObjectID
        :param gas: The gas ObjectID that transaction fees will come from
        :type gas: ObjectID
        :param gas_budget: The gas budget to use in validating transaction payment
        :type gas_budget: SuiInteger
        :param recipient: The receiving party SuiAddress
        :type recipient: SuiAddress
        :return: Result of the transaction
        :rtype: SuiRpcResult
        """
        logger.warning("using deprecated method: transfer_object_txn")
        inargs: dict = locals().copy()
        inargs.pop("self")
        return self.execute(TransferObject(**inargs))

    @deprecated(version="0.28.0", reason="Use SuiTransaction builder instead.")
    def merge_coin_txn(
        self,
        *,
        signer: SuiAddress,
        primary_coin: ObjectID,
        coin_to_merge: ObjectID,
        gas: ObjectID,
        gas_budget: SuiInteger,
    ) -> SuiRpcResult:
        """merge_coin_txn invokes `sui_mergeCoin` API.

        :param signer: Transaction signer and owner of primary and coin being merged
        :type signer: SuiAddress
        :param primary_coin: the coin object to merge into, this coin will remain after the transaction
        :type primary_coin: ObjectID
        :param coin_to_merge: the coin object to be merged, this coin will be destroyed,
            the balance will be added to `primary_coin`
        :type coin_to_merge: ObjectID
        :param gas: gas object to be used in this transaction
        :type gas: ObjectID
        :param gas_budget: the gas budget, the transaction will fail if the gas cost exceed the budget
        :type gas_budget: SuiInteger
        :return: Result of the transaction
        :rtype: SuiRpcResult
        """
        logger.warning("using deprecated method: merge_coin_txn")
        inargs: dict = locals().copy()
        inargs.pop("self")
        return self.execute(MergeCoin(**inargs))

    @deprecated(version="0.28.0", reason="Use SuiTransaction builder instead.")
    def split_coin_txn(
        self,
        *,
        signer: SuiAddress,
        coin_object_id: ObjectID,
        split_amounts: SuiArray[SuiInteger],
        gas: ObjectID,
        gas_budget: SuiInteger,
    ) -> SuiRpcResult:
        """split_coin_txn invokes `sui_splitCoin` API.

        :param signer: Transaction signer and owner of coin being split
        :type signer: SuiAddress
        :param coin_object_id: the coin object to be spilt
        :type coin_object_id: ObjectID
        :param split_amounts: the amounts to split out from the coin
        :type split_amounts: SuiArray[SuiInteger]
        :param gas: gas object to be used in this transaction
        :type gas: ObjectID
        :param gas_budget: the gas budget, the transaction will fail if the gas cost exceed the budget
        :type gas_budget: SuiInteger
        :return: Result of the transaction
        :rtype: SuiRpcResult
        """
        logger.warning("using deprecated method: split_coin_txn")
        inargs: dict = locals().copy()
        inargs.pop("self")
        return self.execute(SplitCoin(**inargs))

    @deprecated(version="0.28.0", reason="Use SuiTransaction builder instead.")
    def split_coin_equally_txn(
        self,
        *,
        signer: SuiAddress,
        coin_object_id: ObjectID,
        split_count: SuiInteger,
        gas: ObjectID,
        gas_budget: SuiInteger,
    ) -> SuiRpcResult:
        """split_coin_equally_txn invokes `sui_splitCoinEqual` API.

        :param signer: Transaction signer and owner of coin being split
        :type signer: SuiAddress
        :param coin_object_id: the coin object to be spilt
        :type coin_object_id: ObjectID
        :param split_count: The count of coins to distribute evenly from coin_object_id
        :type split_count: SuiInteger
        :param gas: gas object to be used in this transaction
        :type gas: ObjectID
        :param gas_budget: the gas budget, the transaction will fail if the gas cost exceed the budget
        :type gas_budget: SuiInteger
        :return: Result of the transaction
        :rtype: SuiRpcResult
        """
        logger.warning("using deprecated method: split_coin_equally_txn")
        inargs: dict = locals().copy()
        inargs.pop("self")
        return self.execute(SplitCoinEqually(**inargs))

    @deprecated(version="0.28.0", reason="Use SuiTransaction builder instead.")
    def move_call_txn(
        self,
        *,
        signer: SuiAddress,
        package_object_id: ObjectID,
        module: SuiString,
        function: SuiString,
        type_arguments: SuiArray[SuiString],
        arguments: SuiArray[SuiString],
        gas: ObjectID,
        gas_budget: SuiInteger,
    ) -> SuiRpcResult:
        """move_call_txn invokes `sui_moveCall` API.

        :param signer: Transaction signer
        :type signer: SuiAddress
        :param package_object_id: the Move package ID
        :type package_object_id: ObjectID
        :param module: The Sui Move module name.
        :type module: SuiString
        :param function: The Sui Move function name
        :type function: SuiString
        :param type_arguments: The type arguments, if any, of the Sui Move function
        :type type_arguments: SuiArray[SuiString]
        :param arguments: The arguments to be passed into the Sui Move function
        :type arguments: SuiArray[SuiString]
        :param gas: gas object to be used in this transaction
        :type gas: ObjectID
        :param gas_budget: the gas budget, the transaction will fail if the gas cost exceed the budget
        :type gas_budget: SuiInteger
        :return: Result of the transaction
        :rtype: SuiRpcResult
        """
        logger.warning("using deprecated method: move_call_txn")
        inargs: dict = locals().copy()
        inargs.pop("self")
        return self.execute(MoveCall(**inargs))

    @deprecated(version="0.28.0", reason="Use SuiTransaction builder instead.")
    def publish_package_txn(
        self,
        sender: SuiAddress,
        compiled_modules: SuiArray[SuiString],
        dependencies: SuiArray[ObjectID],
        gas: ObjectID,
        gas_budget: SuiInteger,
    ) -> SuiRpcResult:
        """publish_package_txn invokes `sui_publish` API.

        :param sender: the transaction signer's Sui address
        :type sender: SuiAddress
        :param compiled_modules: the compiled bytes of a sui move package
        :type compiled_modules: SuiArray[SuiString]
        :param gas: gas object to be used in this transaction
        :type gas: ObjectID
        :param gas_budget: the gas budget, the transaction will fail if the gas cost exceed the budget
        :type gas_budget: SuiInteger
        :return: Result of the transaction
        :rtype: SuiRpcResult
        """
        logger.warning("using deprecated method: publish_package_txn")
        inargs: dict = locals().copy()
        inargs.pop("self")
        return self.execute(Publish(**inargs))
