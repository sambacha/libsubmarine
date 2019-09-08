import json
import time

import logbook as logbook
import rlp
from ethereum import transactions
from ethereum.utils import normalize_address
from web3 import Web3, HTTPProvider

from generate_commitment.generate_submarine_commit import generateCommitAddress
from test_chicken.test_utils import keccak_256_encript_uint32, generate_proof_blob, rec_bin, rec_hex

OURGASLIMIT = 3712394
OURGASPRICE = 10**9
BASIC_SEND_GAS_LIMIT = 3712394
REVEAL_GAS_LIMIT = 1000000
SELECT_WIN_GAS_LIMIT = 4000000
FINALIZE_GAS_LIMIT = 4000000
CHAIN_ID = 42    # mainNet = 1 | Ropsten = 3 | Rinkeby = 4 | Goerli = 5 | Kovan = 42
_logger = logbook.Logger(__name__)

# web3.py instance
# w3 = Web3(HTTPProvider("https://kovan.infura.io/v3/6a78ce7bbca14f73a8644c43eed4d2af"))
# print(w3.isConnected())


# private_key = "0xbdf5bd75f8907a1f5a34d3b1b4fddb047d4cdd71203f3301b5f230dfc1cffa7a" #"<Private Key here with 0x prefix>"
#user_account = w3.eth.account.privateKeyToAccount(private_key)

#wallet_private_key = [WALLET_PRIVATE_KEY]
#wallet_address = [WALLET_ADDRESS]

# load deployed contract
# truffleFile = json.load(open('../contracts/deployed_contracts/ChickenSubmarine.json'))
# abi = truffleFile['abi']
# bytecode = truffleFile['bytecode']
# contract_address = truffleFile['address']
# contract = w3.eth.contract(address=contract_address, bytecode=bytecode, abi=abi)
#contract_ = w3.eth.contract(abi=contract_interface['abi'], bytecode=contract_interface['bin'])


class Player:

    def __init__(self, private_key, infura_url, game_obj):
        self.private_key = private_key
        self.w3 = Web3(HTTPProvider(infura_url))
        assert self.w3.isConnected(), f"Infura connection problem at {infura_url}"
        self.user_account = self.w3.eth.account.privateKeyToAccount(self.private_key)
        self.game_obj = game_obj
        self.game_contract = game_obj.contract
        self.game_address = game_obj.contract.address
        self.submarin_address_b = None
        self.submarine_commit = None
        self.submarine_witness = None
        self.submarine_unlock_tx = None
        self.submarine_tx_receipt = None
        self.reveal_tx_receipt = None
        self.bet_amount_in_wei = None

    ############################
    # Player functions
    ############################

    def send_ether_to_submarine(self, amount_in_wei):
        """
        generate submarine commitment to the game contract with the amount_in_wei bet.
        sent the submarine transaction to the generated sub address
        stores the submarine and the trans receipt
        """

        self.submarin_address_b, self.submarine_commit, self.submarine_witness, self.submarine_unlock_tx = \
            generateCommitAddress(normalize_address(self.user_account.address),
                                  normalize_address(self.game_address),
                                  amount_in_wei, b"",
                                  OURGASPRICE, BASIC_SEND_GAS_LIMIT)

        nonce = self.w3.eth.getTransactionCount(self.user_account.address)
        # Save for the reveal
        self.bet_amount_in_wei = amount_in_wei

        tx_dict = {
            'to': self.submarin_address_b,
            'value': amount_in_wei,
            'gas': BASIC_SEND_GAS_LIMIT,
            'gasPrice': OURGASPRICE,
            'nonce': nonce,
            'chainId': CHAIN_ID
        }

        signed_tx = self.user_account.signTransaction(tx_dict)

        tx_hash = self.w3.eth.sendRawTransaction(signed_tx.rawTransaction)
        self.submarine_tx_receipt = self.w3.eth.waitForTransactionReceipt(tx_hash)

        if self.submarine_tx_receipt is None:
            return {'status': 'failed', 'error': 'timeout'}
        return{'status': 'added', 'tx_receipt': self.submarine_tx_receipt}

    def submarine_reveal_and_unlock(self):
        """
        Send submarine reveal to the game contract
        """
        commit_tx_block_num = self.submarine_tx_receipt.blockNumber
        nonce = self.w3.eth.getTransactionCount(self.user_account.address)
        proof_blob = generate_proof_blob(self)
        signed_unlock_tx = self._create_unlock_tx()

        reveal_tx_dict = self.game_contract.functions.reveal(
            commit_tx_block_num,
            b'',  # unlock extra data - we have none
            rec_bin(self.submarine_witness),
            self.unlock_tx_unsigned_rlp,
            proof_blob).\
            buildTransaction({
                              'chainId': CHAIN_ID,
                              'gas': REVEAL_GAS_LIMIT,
                              'gasPrice': OURGASPRICE,
                              'nonce': nonce})

        _logger.info("Send reveal transaction")
        signed_reveal_tx = self.user_account.signTransaction(reveal_tx_dict)
        reveal_tx_hash = self.w3.eth.sendRawTransaction(signed_reveal_tx.rawTransaction)
        self.reveal_tx_receipt = self.w3.eth.waitForTransactionReceipt(reveal_tx_hash)

        if self.reveal_tx_receipt is None:
            _logger.warn(f"Reveal transaction failed")
            return {'status': 'failed', 'error': 'timeout'}
        _logger.debug(f"Reveal transaction was sent: {self.reveal_tx_receipt}")

        _logger.info(f"send unlock transaction")

        unlock_tx_hash = self.w3.eth.sendRawTransaction(signed_unlock_tx.rawTransaction)
        self.reveal_tx_receipt = self.w3.eth.waitForTransactionReceipt(unlock_tx_hash)

    def finalize(self):
        """
        Collect reword from game contract
        call finalize on the submarine, recive the money from the game contract
        """
        nonce = self.w3.eth.getTransactionCount(self.user_account.address)
        tx_dict = self.game_contract.functions.finalize(rec_bin(self.submarine_commit)).buildTransaction({
            'chainId': CHAIN_ID,
            'gas': FINALIZE_GAS_LIMIT,
            'gasPrice': OURGASPRICE,
            'nonce': nonce,
        })

        signed_tx = self.user_account.signTransaction(tx_dict)
        tx_hash = self.w3.eth.sendRawTransaction(signed_tx.rawTransaction)
        tx_receipt = self.w3.eth.waitForTransactionReceipt(tx_hash)

        if tx_receipt is None:
            return {'status': 'failed', 'error': 'timeout'}
        return {'status': 'added', 'processed_receipt': tx_receipt}

    def _create_unlock_tx(self):
        unlock_tx_info = rlp.decode(rec_bin(self.submarine_unlock_tx))
        _logger.info(f"Unlock tx hex object: {rec_hex(unlock_tx_info)}")

        unlock_tx_object_dict = {
            'to': unlock_tx_info[3],
            'value': self.bet_amount_in_wei,
            'gas': BASIC_SEND_GAS_LIMIT,
            'gasPrice': OURGASPRICE,
            'nonce': self.w3.eth.getTransactionCount(self.user_account.address),
            'chainId': CHAIN_ID,
        }

        signed_unlock_tx = self.user_account.signTransaction(unlock_tx_object_dict)

        # todo - understand how to create this unsigned transaction,
        #  maybe we can use the original submarine_unlock_tx?
        _logger.info(f"Unlock tx hash: {rec_hex(self.unlock_tx_object.hash)}")
        self.unlock_tx_unsigned_object = transactions.UnsignedTransaction(
            int.from_bytes(unlock_tx_info[0], byteorder="big"),  # nonce;
            int.from_bytes(unlock_tx_info[1], byteorder="big"),  # gasprice
            int.from_bytes(unlock_tx_info[2], byteorder="big"),  # startgas
            unlock_tx_info[3],  # to addr
            int.from_bytes(unlock_tx_info[4], byteorder="big"),  # value
            unlock_tx_info[5],  # data
        )

        self.unlock_tx_unsigned_rlp = rlp.encode(self.unlock_tx_unsigned_object, transactions.UnsignedTransaction)
        return signed_unlock_tx